"""Daily Markdown digest — one file per Eastern day, one section per run.

Every run appends a section, including a run that was deferred, throttled or
only read part of what it meant to. Such a run is headed HEALTH_DEGRADED and
lists what it could not see; it is never allowed to read as "no deals".

Every figure in an alert line is a value this install recorded. A field the
API did not give is written as unknown, never filled in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hd.config import Settings
from hd.logging import get_logger

log = get_logger("pipeline.digest")

DIGEST_TZ = ZoneInfo("America/New_York")

# What the digest lists. OOS and the health alert types are left out: health is
# the section header's job, and an item selling out is not a deal.
_LISTED_TYPES = (
    "IN_STORE_CLEARANCE", "CLEARANCE", "DEEP_DISCOUNT", "PRICE_DROP",
    "PRICING_ERROR", "SPECIAL_BUY", "BACK_IN_STOCK",
)


@dataclass
class RunReport:
    started: datetime
    mode: str = ""
    stores: list[str] = field(default_factory=list)
    snapshots: int = 0
    requests: int = 0
    # Reasons this run did not see everything. Any entry makes it DEGRADED.
    degraded: list[str] = field(default_factory=list)
    # Coverage facts that are by design, not failures.
    notes: list[str] = field(default_factory=list)
    # False for a run that never opened the database (a deferred run).
    has_alerts: bool = True


def digest_path(settings: Settings, now: datetime) -> Path:
    day = now.astimezone(DIGEST_TZ).strftime("%Y-%m-%d")
    return Path(settings.digest_dir).expanduser() / f"clearance-digest-{day}.md"


def _money(value: Any) -> str | None:
    return None if value is None else f"${float(value):,.2f}"


def _et(ts: datetime | None) -> str:
    if ts is None:
        return "unknown"
    if ts.tzinfo is None:  # SQLite hands timestamps back naive; they are UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(DIGEST_TZ).strftime("%Y-%m-%d %H:%M ET")


def format_alert(row: dict[str, Any]) -> str:
    """One digest line for one alert.

    `row` carries alert_type, store_id, item_id, payload, and optionally
    store_name and retrieved_at (the snapshot's own timestamp).
    """
    p = row.get("payload") or {}
    after = p.get("after") or {}
    before = p.get("before") or {}

    clearance = after.get("clearance_value")
    deal = clearance if clearance is not None else after.get("price_value")
    refs = [
        r for r in (
            after.get("price_original"),
            before.get("price_value"),
            p.get("reference_price"),
            after.get("price_value") if clearance is not None else None,
        )
        if r is not None
    ]
    was = max(refs) if refs else None

    if deal is None:
        price = "price unknown"
    elif was is not None and was > deal:
        pct = round((was - deal) / was * 100)
        price = f"**{_money(deal)}** (was {_money(was)}, {pct}% off, saves {_money(was - deal)})"
    else:
        price = f"**{_money(deal)}** (was-price unknown)"
    if clearance is not None:
        price += " in-store clearance"

    qty = after.get("inventory_qty")
    if qty is not None:
        stock = f"{qty} in stock"
    elif after.get("in_stock") is True:
        stock = "in stock, quantity not reported"
    elif after.get("in_stock") is False:
        stock = "not in stock"
    else:
        stock = "stock unknown"

    store = f"#{row.get('store_id')}"
    if row.get("store_name"):
        store += f" {row['store_name']}"
    title = p.get("product_title") or "title unknown"
    url = p.get("product_url")
    item = f"[{row.get('item_id')}]({url})" if url else str(row.get("item_id"))

    line = (
        f"- {row.get('alert_type')} · {title} · item {item} · store {store} · "
        f"{price} · {stock} · retrieved {_et(row.get('retrieved_at'))}"
    )
    conf = p.get("confidence")
    if conf and conf.get("score") is not None:
        line += f" · stock confidence {conf.get('label', '')} {conf['score']}"
        if conf.get("tags"):
            line += f" ({', '.join(conf['tags'])})"
    ctx = p.get("context") or {}
    if ctx.get("inflated"):
        line += (
            f" · INFLATED: {ctx.get('claimed_pct')}% off the listed price but "
            f"{ctx.get('true_pct')}% off its usual {_money(ctx.get('typical_price'))}"
        )
    return line


def render_section(report: RunReport, rows: list[dict[str, Any]]) -> str:
    """The Markdown for one run."""
    lines = [f"## Run at {_et(report.started)}", ""]
    stores = ", ".join(f"#{s}" for s in report.stores) or "none configured"
    lines.append(
        f"Stores: {stores} · mode: {report.mode or 'unknown'} · "
        f"{report.snapshots} price readings · {report.requests} requests"
    )
    lines.append("")

    if report.degraded:
        lines.append("**HEALTH_DEGRADED — this run did not see everything:**")
        lines.extend(f"- {reason}" for reason in report.degraded)
        lines.append("")

    if rows:
        lines.append(f"### {len(rows)} new alert(s)")
        lines.extend(format_alert(r) for r in rows)
    elif report.degraded:
        lines.append(
            "No new alerts were recorded, but coverage was incomplete — "
            "this does not mean there are no deals."
        )
    else:
        lines.append("No changes since the previous run.")

    if report.notes:
        lines.append("")
        lines.append("Coverage notes:")
        lines.extend(f"- {note}" for note in report.notes)
    lines.append("")
    return "\n".join(lines) + "\n"


async def load_run_alerts(settings: Settings, since: datetime) -> list[dict[str, Any]]:
    """Alerts written since `since`, each with its snapshot's own timestamp."""
    from sqlalchemy import func, select

    from hd.db.base import get_session
    from hd.db.models import Alert, Store, StoreSnapshot

    rows: list[dict[str, Any]] = []
    async with get_session(settings) as session:
        store_names = {
            s.store_id: s.name for s in (await session.execute(select(Store))).scalars()
        }
        alerts = (
            await session.execute(
                select(Alert).where(Alert.ts >= since).order_by(Alert.store_id, Alert.id)
            )
        ).scalars().all()
        for a in alerts:
            kind = a.alert_type.value if hasattr(a.alert_type, "value") else str(a.alert_type)
            if kind not in _LISTED_TYPES:
                continue
            retrieved = (
                await session.execute(
                    select(func.max(StoreSnapshot.ts)).where(
                        StoreSnapshot.store_id == a.store_id,
                        StoreSnapshot.item_id == a.item_id,
                    )
                )
            ).scalar()
            rows.append({
                "alert_type": kind,
                "store_id": a.store_id,
                "store_name": store_names.get(a.store_id),
                "item_id": a.item_id,
                "payload": a.payload,
                "retrieved_at": retrieved,
            })
    rows.sort(key=lambda r: (r["store_id"], _LISTED_TYPES.index(r["alert_type"])))
    return rows


async def previously_seen(settings: Settings, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which (store, item) pairs this install has ever recorded a reading for."""
    if not pairs:
        return set()
    from sqlalchemy import select

    from hd.db.base import get_session
    from hd.db.models import StoreSnapshot

    seen: set[tuple[str, str]] = set()
    async with get_session(settings) as session:
        for store_id, item_id in pairs:
            hit = (
                await session.execute(
                    select(StoreSnapshot.id).where(
                        StoreSnapshot.store_id == store_id,
                        StoreSnapshot.item_id == item_id,
                    ).limit(1)
                )
            ).first()
            if hit:
                seen.add((store_id, item_id))
    return seen


async def write_digest(settings: Settings, report: RunReport) -> Path | None:
    """Append this run's section to today's digest file. Returns its path."""
    if not settings.digest_enabled:
        return None
    rows = await load_run_alerts(settings, report.started) if report.has_alerts else []
    path = digest_path(settings, report.started)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as fh:
        if new_file:
            day = report.started.astimezone(DIGEST_TZ).strftime("%Y-%m-%d")
            fh.write(f"# Clearance digest — {day}\n\n")
        fh.write(render_section(report, rows))
    log.info("Digest written", path=str(path), alerts=len(rows), degraded=bool(report.degraded))
    return path
