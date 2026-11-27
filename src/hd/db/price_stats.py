"""Maintenance of item_price_stats, the price history that survives pruning.

One pure fold (`apply_observation`) is the single definition of how an
observation changes an item's running facts. Both callers use it — the write
path as snapshots land, and the backfill that reconstructs the table from raw
history — so the two can never drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

import structlog
from sqlalchemy import delete, select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import ItemPriceStat, StoreSnapshot

log = structlog.get_logger(__name__)

# SQLite caps bound parameters per statement; chunk IN () lookups below it
_IN_CHUNK = 500


def _naive_utc(ts: datetime) -> datetime:
    """Normalize to naive UTC so stored and in-flight timestamps compare."""
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def apply_observation(
    stat: ItemPriceStat, price: Decimal | None, ts: datetime
) -> None:
    """Fold one priced observation into an item's running stats.

    Assumes observations arrive in time order, which both callers guarantee —
    the write path appends at `now`, the backfill streams ordered by ts. Out of
    order input can only overcount obs_days; the price extremes stay correct.
    """
    if price is None:
        return
    ts = _naive_utc(ts)

    # A fresh ORM object has no column defaults applied until flush
    if stat.obs_count is None:
        stat.obs_count = 0
    if stat.obs_days is None:
        stat.obs_days = 0
    if stat.price_sum is None:
        stat.price_sum = Decimal("0")

    if stat.low_price is None or price < stat.low_price:
        stat.low_price, stat.low_ts = price, ts
    if stat.high_price is None or price > stat.high_price:
        stat.high_price, stat.high_ts = price, ts

    stat.price_sum = stat.price_sum + price
    stat.obs_count += 1

    # Count the day before last_ts moves — six scans in one afternoon are one day
    if stat.last_ts is None or ts.date() != _naive_utc(stat.last_ts).date():
        stat.obs_days += 1

    if stat.first_ts is None or ts < _naive_utc(stat.first_ts):
        stat.first_ts = ts
    if stat.last_ts is None or ts > _naive_utc(stat.last_ts):
        stat.last_ts = ts


def mean_price(stat: ItemPriceStat) -> float | None:
    """The average price behind this item, or None if nothing was observed."""
    if not stat.obs_count or stat.price_sum is None:
        return None
    return float(stat.price_sum) / stat.obs_count


async def record_observations(
    session, store_id: str, observations: Sequence[tuple[str, Decimal | None, datetime]]
) -> int:
    """Fold a batch of (item_id, price, ts) into the aggregate, in-session.

    Runs inside the caller's transaction so the aggregate commits with the
    snapshots that produced it — a half-applied batch would leave the stats
    permanently understating history no later pass could detect.
    """
    priced = [(i, p, t) for i, p, t in observations if p is not None]
    if not priced:
        return 0

    item_ids = sorted({i for i, _, _ in priced})
    by_id: dict[str, ItemPriceStat] = {}
    for start in range(0, len(item_ids), _IN_CHUNK):
        chunk = item_ids[start:start + _IN_CHUNK]
        rows = await session.execute(
            select(ItemPriceStat).where(
                ItemPriceStat.store_id == store_id,
                ItemPriceStat.item_id.in_(chunk),
            )
        )
        for stat in rows.scalars():
            by_id[stat.item_id] = stat

    for item_id, price, ts in priced:
        stat = by_id.get(item_id)
        if stat is None:
            stat = ItemPriceStat(
                store_id=store_id, item_id=item_id,
                price_sum=Decimal("0"), obs_count=0, obs_days=0,
            )
            session.add(stat)
            by_id[item_id] = stat
        apply_observation(stat, price, ts)

    return len(priced)


async def backfill(settings: Settings, chunk_size: int = 50_000) -> tuple[int, int]:
    """Rebuild the whole aggregate from surviving raw snapshots.

    Safe to run only while raw history exists — it is a reconstruction, not a
    merge, so it replaces the table wholesale. Reads just the four columns it
    needs, leaving the ~1 GB of raw_json untouched on disk.
    """
    folded: dict[tuple[str, str], ItemPriceStat] = {}
    scanned = 0
    last_id = 0

    async with get_session(settings) as session:
        while True:
            rows = (await session.execute(
                select(
                    StoreSnapshot.id,
                    StoreSnapshot.store_id,
                    StoreSnapshot.item_id,
                    StoreSnapshot.price_value,
                    StoreSnapshot.ts,
                )
                .where(StoreSnapshot.id > last_id, StoreSnapshot.price_value.isnot(None))
                .order_by(StoreSnapshot.id)
                .limit(chunk_size)
            )).all()
            if not rows:
                break
            for row_id, store_id, item_id, price, ts in rows:
                key = (store_id, item_id)
                stat = folded.get(key)
                if stat is None:
                    stat = ItemPriceStat(
                        store_id=store_id, item_id=item_id,
                        price_sum=Decimal("0"), obs_count=0, obs_days=0,
                    )
                    folded[key] = stat
                apply_observation(stat, price, ts)
                last_id = row_id
            scanned += len(rows)
            log.info("Backfill progress", scanned=scanned, items=len(folded))

    async with get_session(settings) as session:
        await session.execute(delete(ItemPriceStat))
        for stat in folded.values():
            session.add(stat)

    log.info("Backfill complete", scanned=scanned, items=len(folded))
    return scanned, len(folded)


# --- markdown cadence and true-discount context ------------------------------
#
# Home Depot encodes how far along a markdown is in the last two digits of the
# price. The ladder runs .00 (regular) -> .06 -> .03 -> .02 -> .01. A pricing
# convention observed by shoppers, not something the API states, so it is
# reported as a hint ("ending .03") and never as a fact about the item.

from statistics import median as _median

MARKDOWN_ENDINGS: dict[int, tuple[int, str]] = {
    0: (0, "regular"),
    6: (1, "first markdown"),
    3: (2, "second markdown"),
    2: (3, "deep markdown"),
    1: (4, "final markdown"),
}

# A claimed discount this many points deeper than the real one is "inflated".
INFLATED_GAP_POINTS = 20


def markdown_ending(price: float | Decimal | None) -> dict | None:
    """Cadence stage implied by a price's cents, or None when it follows no ladder."""
    if price is None:
        return None
    cents = int(round(float(price) * 100)) % 100
    last_two = cents
    if last_two in MARKDOWN_ENDINGS:
        stage, label = MARKDOWN_ENDINGS[last_two]
        return {"ending": f".{last_two:02d}", "stage": stage, "label": label}
    return {"ending": f".{last_two:02d}", "stage": None, "label": "off-ladder"}


def days_at_current_price(
    readings: Sequence[tuple[datetime, float | Decimal | None]],
    now: datetime | None = None,
) -> int | None:
    """Whole days the latest price has been unchanged, by the readings we hold.

    A lower bound: it counts from the first reading at this price, and we may
    have started watching after the price was set.
    """
    priced = sorted(
        ((_naive_utc(t), float(p)) for t, p in readings if p is not None),
        key=lambda pair: pair[0],
    )
    if not priced:
        return None
    current = priced[-1][1]
    since = priced[-1][0]
    for ts, price in reversed(priced):
        if abs(price - current) > 0.005:
            break
        since = ts
    ref = _naive_utc(now) if now else _naive_utc(datetime.now(timezone.utc))
    return max(0, (ref - since).days)


def median_price(prices: Sequence[float | Decimal | None]) -> float | None:
    values = [float(p) for p in prices if p is not None]
    return float(_median(values)) if values else None


def discount_context(
    current: float | Decimal | None,
    msrp: float | Decimal | None,
    typical: float | Decimal | None,
    sample_days: int | None = None,
) -> dict:
    """How the headline discount compares with what the item usually sells for.

    `typical` should be the median of readings we hold (capped to the raw
    window we still have); the label says how many days that covers. When the
    claimed discount off MSRP is far deeper than the discount off the typical
    price, the markdown is flagged as inflated: the "was" price is not a price
    anyone was paying.
    """
    if current is None:
        return {"claimed_pct": None, "true_pct": None, "inflated": False}
    cur = float(current)
    claimed = None
    if msrp and float(msrp) > 0 and float(msrp) > cur:
        claimed = round((float(msrp) - cur) / float(msrp) * 100)
    true = None
    if typical and float(typical) > 0:
        true = round((float(typical) - cur) / float(typical) * 100)
    inflated = bool(
        claimed is not None and true is not None and claimed - true >= INFLATED_GAP_POINTS
    )
    return {
        "claimed_pct": claimed,
        "true_pct": true,
        "typical_price": round(float(typical), 2) if typical else None,
        "typical_days": sample_days,
        "inflated": inflated,
    }
