"""Load the evidence for each deal and score it.

Reads recent snapshots (for stock velocity, peers and price history) and the
latest pickup state, then hands pure inputs to hd.pipeline.confidence and the
price-context helpers. Read-only: nothing here writes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import ItemFulfillment, StoreSnapshot
from hd.db.price_stats import (
    days_at_current_price,
    discount_context,
    markdown_ending,
    median_price,
)
from hd.pipeline.confidence import StockReading, score_stock

_CHUNK = 400


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def assess_deals(
    settings: Settings,
    deals: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Confidence, cadence and price context keyed by (store_id, item_id).

    Each deal needs: store_id, item_id, title, qty, in_stock, clearance_value,
    online_price, and optionally msrp (price_original).
    """
    deals = list(deals)
    if not deals:
        return {}
    now = _aware(now or datetime.now(timezone.utc))
    horizon = now - timedelta(days=settings.confidence_history_days)
    item_ids = sorted({d["item_id"] for d in deals})
    stores = sorted(set(settings.store_list) | {d["store_id"] for d in deals})

    rows: list[StoreSnapshot] = []
    fulfillment: dict[tuple[str, str], str | None] = {}
    async with get_session(settings) as session:
        for i in range(0, len(item_ids), _CHUNK):
            chunk = item_ids[i:i + _CHUNK]
            rows.extend(
                (await session.execute(
                    select(StoreSnapshot).where(
                        StoreSnapshot.item_id.in_(chunk),
                        StoreSnapshot.store_id.in_(stores),
                        StoreSnapshot.ts >= horizon,
                    )
                )).scalars()
            )
            for f in (await session.execute(
                select(ItemFulfillment).where(
                    ItemFulfillment.item_id.in_(chunk),
                    ItemFulfillment.store_id.in_(stores),
                )
            )).scalars():
                fulfillment[(f.store_id, f.item_id)] = f.bopis_state

    by_pair: dict[tuple[str, str], list[StoreSnapshot]] = defaultdict(list)
    for r in rows:
        by_pair[(r.store_id, r.item_id)].append(r)
    for series in by_pair.values():
        series.sort(key=lambda r: _aware(r.ts))

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for d in deals:
        store, item = d["store_id"], d["item_id"]
        series = by_pair.get((store, item), [])
        history = [StockReading(_aware(r.ts), r.inventory_qty, r.in_stock) for r in series]

        peers: list[int | None] = []
        for other in stores:
            if other == store:
                continue
            latest = (by_pair.get((other, item)) or [None])[-1]
            if latest is not None and latest.clearance_value is not None:
                peers.append(latest.inventory_qty)

        latest_row = series[-1] if series else None
        confidence = score_stock(
            qty=d.get("qty"),
            in_stock=d.get("in_stock"),
            limited=latest_row.limited_qty if latest_row else None,
            bopis_state=fulfillment.get((store, item)),
            title=d.get("title"),
            history=history,
            peer_qtys=peers,
            stores_watched=len(stores),
            now=now,
        )

        effective = [
            (_aware(r.ts), float(r.clearance_value if r.clearance_value is not None else r.price_value)
             if (r.clearance_value is not None or r.price_value is not None) else None)
            for r in series
        ]
        regular_prices = [r.price_value for r in series]
        span_days = (
            (_aware(series[-1].ts) - _aware(series[0].ts)).days + 1 if series else None
        )
        msrp = d.get("msrp") if d.get("msrp") is not None else (
            float(latest_row.price_original) if latest_row and latest_row.price_original else None
        )
        out[(store, item)] = {
            "confidence": confidence.as_dict(),
            "cadence": {
                **(markdown_ending(d.get("clearance_value")) or {}),
                "days_at_price": days_at_current_price(effective, now),
            },
            "context": discount_context(
                d.get("clearance_value"), msrp, median_price(regular_prices), span_days
            ),
        }
    return out
