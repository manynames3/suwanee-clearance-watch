"""Tiered polling: hot every run, warm on rotation, cold only when due.

* **Hot** — the SKU watchlist and anything already on clearance. Cheap, and it
  runs every time (see pipeline/watchlist.py and the daily-deals sweep).
* **Warm** — the in-store *shelf* tier. It already rotates through categories
  on its own cursor, so it is left alone here.
* **Cold** — the online *network* tier: the whole brand catalogue at
  storefilter=ALL, thousands of items and by far the costliest walk. It moves
  slowly, so it is gated to once per `network_tier_min_interval_hours`.

The gate only ever removes the cold tier from a run. It never adds work, never
touches the shelf tier, and fails open: a missing or unreadable state file
means "never ran", so the tier runs rather than being silently starved.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hd.config import Settings
from hd.logging import get_logger

log = get_logger("pipeline.tiering")

COLD_TIER = "network"


def _read(path: str | Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def last_cold_completion(settings: Settings) -> datetime | None:
    raw = _read(settings.tier_state_path).get("network_completed_at")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def cold_tier_due(settings: Settings, now: datetime | None = None) -> bool:
    interval = settings.network_tier_min_interval_hours
    if not settings.tiered_polling or interval <= 0:
        return True
    last = last_cold_completion(settings)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - last >= timedelta(hours=interval)


def gate_tiers(
    settings: Settings, tiers: tuple[str, ...], now: datetime | None = None
) -> tuple[str, ...]:
    """`tiers` minus the cold tier when it ran recently enough."""
    if COLD_TIER in tiers and not cold_tier_due(settings, now):
        log.info(
            "Cold tier not due — skipping the online network walk",
            interval_hours=settings.network_tier_min_interval_hours,
        )
        return tuple(t for t in tiers if t != COLD_TIER)
    return tiers


def mark_cold_completed(settings: Settings, now: datetime | None = None) -> None:
    """Record a *clean* cold-tier pass. Never called for a cut-short run."""
    when = (now or datetime.now(timezone.utc)).isoformat()
    path = Path(settings.tier_state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**_read(path), "network_completed_at": when}))
    except OSError as exc:
        log.warning("Could not persist tier state", error=str(exc))
