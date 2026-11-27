"""Phase 2 through the real database: board filtering, alerts, pickup state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio

from hd.config import Settings
from hd.dashboard.queries import get_deal_board
from hd.db.base import Database
from hd.db.models import (
    Alert, AlertType, ItemFulfillment, Product, Severity, Store, StoreSnapshot,
)
from hd.pipeline.alerts import write_alerts
from hd.pipeline.snapshot import _insert_snapshots
from hd.hd_api.models import NormalizedSnapshot

NOW = datetime.now(timezone.utc)


def make_settings(**kw) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        stores="1754,6978",
        brands="Milwaukee",
        store_raw_json=False,
        **kw,
    )


@pytest_asyncio.fixture
async def db():
    from hd.db import base as db_base

    s = make_settings()
    db_base._default = Database()
    await db_base._default.init_db(s)
    async with db_base._default.get_session(s) as session:
        session.add(Store(store_id="1754", name="Suwanee"))
        session.add(Store(store_id="6978", name="Brookwood"))
        session.add(Product(item_id="111", brand="X", title="Cordless Drill", is_active=True))
        session.add(Product(item_id="222", brand="X", title="Pre-lit Christmas Tree", is_active=True))
        for item, qty in (("111", 6), ("222", 1)):
            session.add(StoreSnapshot(
                store_id="1754", item_id=item, ts=NOW - timedelta(minutes=5),
                price_value=Decimal("100.00"), price_original=Decimal("100.00"),
                clearance_value=Decimal("40.00"), clearance_percentage_off=60,
                in_stock=True, inventory_qty=qty,
            ))
    return s


async def test_board_hides_low_confidence_unless_asked(db):
    board = await get_deal_board(db)
    shown = [d["item_id"] for d in board["stores"]["1754"]]
    assert shown == ["111"]
    assert [d["item_id"] for d in board["low_confidence"]["1754"]] == ["222"]

    everything = await get_deal_board(db, include_low_confidence=True)
    assert {d["item_id"] for d in everything["stores"]["1754"]} == {"111", "222"}
    scored = next(d for d in everything["stores"]["1754"] if d["item_id"] == "222")
    assert scored["confidence"]["score"] < 40
    assert scored["cadence"]["ending"] == ".00"


async def test_no_bs_can_be_switched_off(db):
    off = make_settings(no_bs_mode=False)
    board = await get_deal_board(off)
    assert {d["item_id"] for d in board["stores"]["1754"]} == {"111", "222"}
    assert all(v == [] for v in board["low_confidence"].values())


def alert(item, qty, kind=AlertType.IN_STORE_CLEARANCE):
    return Alert(
        store_id="1754", item_id=item, alert_type=kind, severity=Severity.HIGH,
        payload={"after": {"clearance_value": 40.0, "price_value": 100.0,
                           "inventory_qty": qty, "in_stock": True}},
    )


async def test_alerts_drop_ghost_stock_keep_real_and_stamp_evidence(db):
    from sqlalchemy import select
    from hd.db import base as db_base

    written = await write_alerts(db, [alert("111", 6), alert("222", 1)])
    assert written == 1
    async with db_base._default.get_session(db) as session:
        rows = (await session.execute(select(Alert))).scalars().all()
    assert [r.item_id for r in rows] == ["111"]
    assert rows[0].payload["confidence"]["score"] >= 40


async def test_stock_alerts_are_never_judged(db):
    written = await write_alerts(db, [alert("222", 1, kind=AlertType.OOS)])
    assert written == 1


async def test_pickup_state_is_persisted_and_updated(db):
    from sqlalchemy import select
    from hd.db import base as db_base

    snap = NormalizedSnapshot(item_id="111", store_id="1754", price_value=10.0,
                              inventory_qty=3, bopis_state="active")
    await _insert_snapshots(db, [snap], "1754", NOW)
    snap2 = NormalizedSnapshot(item_id="111", store_id="1754", price_value=10.0,
                               inventory_qty=2, bopis_state="limited")
    await _insert_snapshots(db, [snap2], "1754", NOW + timedelta(hours=1))
    async with db_base._default.get_session(db) as session:
        rows = (await session.execute(select(ItemFulfillment))).scalars().all()
    assert [(r.item_id, r.bopis_state) for r in rows] == [("111", "limited")]


async def test_site_rows_carry_evidence_and_all_deals(db):
    from hd.pipeline.site import build_site_data

    data = await build_site_data(db, None)
    store = next(s for s in data["stores"] if s["id"] == "1754")
    assert {d["item_id"] for d in store["deals"]} == {"111", "222"}
    assert data["no_bs"] == {"enabled": True, "minimum": 40}
    assert all(d["confidence"] for d in store["deals"])
