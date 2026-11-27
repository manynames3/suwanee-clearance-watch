"""SKU watchlist — price hand-picked item ids directly at every store.

The brand walk only sees the brands and categories it is configured for. An
item named on a community list, or a past find from another brand, is invisible
to it. The watchlist prices those ids one by one through the same searchModel
path the daily-deals sweep uses (keyword=itemId resolves a single item), once
per configured store because clearance is a per-store fact.

Snapshots land in store_snapshots like any other observation, so the ordinary
diff raises the alerts. Nothing here decides what a deal is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hd.config import Settings
from hd.hd_api.graphql import is_valid_search_response, search
from hd.hd_api.parsers import parse_products, parse_snapshots
from hd.http.client import HDClient
from hd.logging import get_logger

log = get_logger("pipeline.watchlist")


@dataclass
class WatchlistSummary:
    listed: int = 0
    # Ids in the file beyond watchlist_max_items, which were not checked.
    capped: int = 0
    # Lines that are not an item id. Reported so a typo is not a silent skip.
    invalid_lines: list[str] = field(default_factory=list)
    lookups: int = 0
    products: int = 0
    snapshots: int = 0
    # (store_id, item_id) pairs the API answered for but did not return.
    not_returned: list[tuple[str, str]] = field(default_factory=list)
    # (store_id, item_id) pairs whose request failed or was never sent.
    failed: list[tuple[str, str]] = field(default_factory=list)
    aborted: bool = False

    @property
    def complete(self) -> bool:
        """True only when every listed item was asked about at every store."""
        return not (self.aborted or self.failed or self.capped)


def read_watchlist(path: str | Path) -> tuple[list[str], list[str]]:
    """Read item ids from the watchlist file.

    Returns (item_ids, invalid_lines). A missing file is an empty list: the
    watchlist is optional. Order is kept and duplicates dropped.
    """
    try:
        text = Path(path).expanduser().read_text()
    except (FileNotFoundError, IsADirectoryError):
        return [], []
    ids: list[str] = []
    invalid: list[str] = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if entry.isdigit():
            if entry not in ids:
                ids.append(entry)
        else:
            invalid.append(entry)
    return ids, invalid


def request_allowance(settings: Settings) -> int:
    """Requests the watchlist will spend this run: one per item per store."""
    ids, _ = read_watchlist(settings.watchlist_path)
    return min(len(ids), max(0, settings.watchlist_max_items)) * len(settings.store_list)


async def lookup_item(
    settings: Settings,
    client: HDClient,
    store_id: str,
    item_id: str,
    now: datetime,
    summary: WatchlistSummary,
) -> str:
    """One authoritative lookup of one item at one store, recorded if returned.

    Returns "ok", "failed" (no usable answer) or "not_returned" (a valid answer
    that did not contain the item). Shared by the watchlist and the Stage-2
    probe so both read an item the same way.
    """
    from hd.pipeline.discovery import _upsert_products
    from hd.pipeline.snapshot import _insert_snapshots

    raw = await search(
        client,
        keyword=item_id,
        nav_param=None,
        store_id=store_id,
        start_index=0,
        page_size=24,
        storefilter="ALL",
    )
    if not is_valid_search_response(raw):
        return "failed"
    summary.lookups += 1

    products = [p for p in parse_products(raw) if p.item_id == item_id]
    snapshots = [s for s in parse_snapshots(raw, store_id) if s.item_id == item_id]
    if not products or not snapshots:
        # A valid answer without the item. Could be a wrong id, could be a
        # degraded read — the digest reports it either way.
        return "not_returned"
    summary.products += await _upsert_products(settings, products)
    summary.snapshots += await _insert_snapshots(settings, snapshots, store_id, now)
    return "ok"


async def run_watchlist(
    settings: Settings,
    client: HDClient,
    store_ids: list[str],
    now: datetime | None = None,
) -> WatchlistSummary:
    """Price every watchlist item at every store and record what was seen."""
    summary = WatchlistSummary()
    all_ids, summary.invalid_lines = read_watchlist(settings.watchlist_path)
    summary.listed = len(all_ids)
    item_ids = all_ids[: max(0, settings.watchlist_max_items)]
    summary.capped = len(all_ids) - len(item_ids)
    if summary.invalid_lines:
        log.warning("Watchlist lines ignored — not item ids", lines=summary.invalid_lines)
    if summary.capped:
        log.warning("Watchlist capped", listed=summary.listed, checked=len(item_ids))
    if not item_ids or not store_ids:
        return summary

    now = now or datetime.now(timezone.utc)
    pairs = [(s, i) for s in store_ids for i in item_ids]
    for n, (store_id, item_id) in enumerate(pairs):
        if client.is_throttled:
            # Everything not yet asked is unseen, and is reported as such.
            summary.aborted = True
            summary.failed.extend(pairs[n:])
            break
        outcome = await lookup_item(settings, client, store_id, item_id, now, summary)
        if outcome == "failed":
            summary.failed.append((store_id, item_id))
        elif outcome == "not_returned":
            summary.not_returned.append((store_id, item_id))

    log.info(
        "Watchlist complete",
        listed=summary.listed, lookups=summary.lookups, snapshots=summary.snapshots,
        not_returned=len(summary.not_returned), failed=len(summary.failed),
        aborted=summary.aborted,
    )
    return summary
