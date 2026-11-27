"""Stage-2 selective probe: re-read the strongest deals just before alerting.

The broad scan is a wide net, and a page can be minutes or hours old by the time
a deal reaches an alert. For the few candidates that matter — the deepest
discounts and brand-new markdowns — this asks for each item on its own, at its
own store, using the same lookup the watchlist uses. That returns fresh stock,
price and pickup state for the price of one request per candidate, and only for
candidates, so total volume stays small (`probe_max_per_run`).

It reads what the verified API returns. Aisle and bay are NOT part of that
response, so this does not claim them; the UPC (already captured) is what an
associate can scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hd.config import Settings
from hd.http.client import HDClient
from hd.logging import get_logger
from hd.pipeline.watchlist import WatchlistSummary, lookup_item

log = get_logger("pipeline.probe")


@dataclass
class ProbeSummary:
    candidates: list[tuple[str, str]] = field(default_factory=list)
    refreshed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    aborted: bool = False


def pick_candidates(
    deals_by_store: dict[str, list[dict[str, Any]]],
    min_percent_off: int,
    limit: int,
) -> list[tuple[str, str]]:
    """The deepest or newest deals, best first, as (store_id, item_id)."""
    pool: list[tuple[int, int, str, str]] = []
    for store_id, deals in deals_by_store.items():
        for d in deals:
            pct = d.get("pct_off") or 0
            if pct >= min_percent_off or d.get("is_new"):
                pool.append((1 if d.get("is_new") else 0, pct, store_id, d["item_id"]))
    pool.sort(key=lambda t: (-t[0], -t[1], t[2], t[3]))
    return [(s, i) for _, _, s, i in pool[: max(0, limit)]]


async def run_probe(
    settings: Settings,
    client: HDClient,
    now: datetime | None = None,
) -> ProbeSummary:
    from hd.db.repository import get_deal_board

    summary = ProbeSummary()
    if settings.probe_max_per_run <= 0 or client.is_throttled:
        return summary
    board = await get_deal_board(settings, include_low_confidence=True)
    summary.candidates = pick_candidates(
        board["stores"], settings.probe_min_percent_off, settings.probe_max_per_run
    )
    now = now or datetime.now(timezone.utc)
    scratch = WatchlistSummary()
    for store_id, item_id in summary.candidates:
        if client.is_throttled:
            summary.aborted = True
            break
        outcome = await lookup_item(settings, client, store_id, item_id, now, scratch)
        if outcome == "ok":
            summary.refreshed += 1
        else:
            summary.failed.append((store_id, item_id))
    log.info("Stage-2 probe complete", candidates=len(summary.candidates),
             refreshed=summary.refreshed, failed=len(summary.failed))
    return summary
