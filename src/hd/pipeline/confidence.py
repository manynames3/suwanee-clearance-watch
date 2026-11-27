"""Real Stock Confidence: how likely is the shelf to actually hold this item?

A clearance price is a claim about the catalogue; the question a shopper has is
whether a unit is physically there. Markdown stock is full of ghosts — units a
system thinks exist that shrinkage or a mis-scan removed long ago. No number can
settle that from a distance, so this score is an *estimate* with its reasoning
spelled out, never a verdict. Every point moved is explained by a tag.

The score starts neutral (50) and moves only on evidence. An item with no
history is not penalised for having none; it simply has fewer reasons to move.

Signals
  * Pickup state — strong boost when online pickup is live; a penalty when stock
    is flagged "limited". NOT penalised: pickup simply being off. HD does not
    sell clearance online, so pickup reads as unavailable for items that are on
    the shelf (see docs/HD_Scanner_Clearance_Report.md).
  * Velocity — stock that is actively falling across recent readings is being
    bought, which proves units exist; stock frozen at 1-3 for ten-plus days is
    the classic ghost.
  * Department floors — seasonal, holiday, patio and garden stock is cleared
    from shelves fast, so it must show more units (or live pickup) to be
    believed; year-round hardware needs only a couple.
  * Cross-store cluster — several watched stores marking the same SKU down with
    real quantity points to a genuine chain-wide markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

BASE_SCORE = 50

# Tunables, kept as named constants so the rules read as policy, not magic.
BOPIS_ACTIVE_BOOST = 20
BOPIS_LIMITED_PENALTY = 25
VELOCITY_BOOST = 15
STAGNATION_PENALTY = 30
STAGNATION_DAYS = 10
STAGNATION_MAX_QTY = 3
STAGNATION_MIN_READINGS = 3
SEASONAL_MIN_QTY = 6
YEAR_ROUND_MIN_QTY = 2
SEASONAL_SHORTFALL_PENALTY = 25
YEAR_ROUND_SHORTFALL_PENALTY = 15
CLUSTER_STORES = 3
CLUSTER_MIN_QTY = 4
CLUSTER_BOOST = 15
OUT_OF_STOCK_SCORE = 0

SEASONAL_WORDS = re.compile(
    r"\b(christmas|holiday|halloween|santa|wreath|garland|ornament|"
    r"patio|pergola|gazebo|umbrella|hammock|grill|smoker|fire ?pit|"
    r"pool|sprinkler|garden|planter|mulch|fertilizer|seed|soil|"
    r"lawn ?mower|snow ?blower|snow ?shovel|ice ?melt|heater|fan|"
    r"outdoor (?:furniture|decor|lighting)|string lights|"
    r"artificial (?:tree|plant)|pre-?lit)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StockReading:
    ts: datetime
    qty: int | None
    in_stock: bool | None = None


@dataclass
class StockConfidence:
    score: int
    label: str
    tags: list[str] = field(default_factory=list)
    department: str = "year_round"

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "label": self.label,
            "tags": list(self.tags),
            "department": self.department,
        }


def classify_department(title: str | None) -> str:
    """'seasonal' when the title names a seasonal/garden/holiday product.

    A title heuristic, and labelled as such in the tags: the catalogue we store
    carries no department, so this is the honest limit of what is knowable.
    """
    return "seasonal" if title and SEASONAL_WORDS.search(title) else "year_round"


def label_for(score: int) -> str:
    if score >= 70:
        return "High"
    if score >= 40:
        return "Medium"
    return "Low"


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _qty_series(history: Sequence[StockReading]) -> list[tuple[datetime, int]]:
    return sorted(
        ((_aware(r.ts), r.qty) for r in history if r.qty is not None),
        key=lambda pair: pair[0],
    )


def _is_falling(series: list[tuple[datetime, int]]) -> bool:
    """True when stock stepped down across the most recent distinct values.

    Needs at least two distinct values, ending lower than they started, and no
    restock in the window: 12 -> 8 -> 5 qualifies; 5 -> 12 -> 8 does not.
    """
    values: list[int] = []
    for _, qty in series:
        if not values or values[-1] != qty:
            values.append(qty)
    recent = values[-4:]
    if len(recent) < 2:
        return False
    return all(b < a for a, b in zip(recent, recent[1:]))


def _is_stagnant(series: list[tuple[datetime, int]], now: datetime) -> bool:
    """Low, unchanging stock observed over ten-plus days."""
    if len(series) < STAGNATION_MIN_READINGS:
        return False
    window_start = now - timedelta(days=STAGNATION_DAYS)
    recent = [(t, q) for t, q in series if t >= window_start]
    # Must have been watching for the whole window, not just seen it twice today.
    if series[0][0] > window_start or len(recent) < STAGNATION_MIN_READINGS:
        return False
    quantities = {q for _, q in recent}
    return len(quantities) == 1 and 1 <= next(iter(quantities)) <= STAGNATION_MAX_QTY


def score_stock(
    *,
    qty: int | None,
    in_stock: bool | None,
    limited: bool | None = None,
    bopis_state: str | None = None,
    title: str | None = None,
    history: Sequence[StockReading] = (),
    peer_qtys: Sequence[int | None] = (),
    stores_watched: int = 1,
    now: datetime | None = None,
) -> StockConfidence:
    """Score one store/SKU. `peer_qtys` are OTHER watched stores' clearance qty."""
    now = _aware(now or datetime.now(timezone.utc))
    department = classify_department(title)
    tags: list[str] = []

    if qty == 0 or in_stock is False:
        return StockConfidence(OUT_OF_STOCK_SCORE, label_for(OUT_OF_STOCK_SCORE),
                               ["out-of-stock"], department)

    score = BASE_SCORE

    # Pickup state
    if bopis_state == "active":
        score += BOPIS_ACTIVE_BOOST
        tags.append("pickup-live +%d" % BOPIS_ACTIVE_BOOST)
    elif bopis_state == "limited" or limited:
        score -= BOPIS_LIMITED_PENALTY
        tags.append("limited-stock -%d" % BOPIS_LIMITED_PENALTY)

    # Velocity vs stagnation
    series = _qty_series(history)
    if _is_falling(series):
        score += VELOCITY_BOOST
        tags.append("stock-falling +%d" % VELOCITY_BOOST)
    elif _is_stagnant(series, now):
        score -= STAGNATION_PENALTY
        tags.append(f"static-low-stock-{STAGNATION_DAYS}d -{STAGNATION_PENALTY}")

    # Department floor
    live_pickup = bopis_state == "active"
    if department == "seasonal":
        tags.append("dept:seasonal (title match)")
        if (qty is None or qty < SEASONAL_MIN_QTY) and not live_pickup:
            score -= SEASONAL_SHORTFALL_PENALTY
            tags.append(f"seasonal-needs-{SEASONAL_MIN_QTY}+ -{SEASONAL_SHORTFALL_PENALTY}")
    elif qty is not None and qty < YEAR_ROUND_MIN_QTY:
        score -= YEAR_ROUND_SHORTFALL_PENALTY
        tags.append(f"single-unit -{YEAR_ROUND_SHORTFALL_PENALTY}")

    # Cross-store cluster
    strong_peers = sum(1 for q in peer_qtys if q is not None and q >= CLUSTER_MIN_QTY)
    this_strong = qty is not None and qty >= CLUSTER_MIN_QTY
    if this_strong and strong_peers + 1 >= CLUSTER_STORES:
        score += CLUSTER_BOOST
        tags.append(f"cluster-{strong_peers + 1}-stores +{CLUSTER_BOOST}")
    elif stores_watched >= CLUSTER_STORES and not peer_qtys:
        tags.append("isolated (no other watched store)")

    score = max(0, min(100, score))
    return StockConfidence(score, label_for(score), tags, department)


def passes_no_bs(confidence: StockConfidence | None, minimum: int) -> bool:
    """No-BS mode keeps anything unscored and anything at or above the minimum."""
    return confidence is None or confidence.score >= minimum
