"""Phase 2: stock confidence, pickup state, markdown cadence, No-BS filtering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hd.db.price_stats import (
    days_at_current_price,
    discount_context,
    markdown_ending,
    median_price,
)
from hd.hd_api.parsers import bopis_state, parse_snapshots
from hd.pipeline.confidence import (
    BASE_SCORE,
    StockReading,
    classify_department,
    passes_no_bs,
    score_stock,
)
from hd.pipeline.digest import format_alert
from hd.pipeline.probe import pick_candidates

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def readings(*pairs):
    """(days_ago, qty) -> StockReading list."""
    return [StockReading(NOW - timedelta(days=d), q) for d, q in pairs]


# ---- pickup state -----------------------------------------------------------


def item(inv, store="1754", service="bopis"):
    return {"fulfillment": {"fulfillmentOptions": [{"type": "pickup", "services": [
        {"type": service, "locations": [{"locationId": store, "inventory": inv}]}]}]}}


def test_bopis_states():
    assert bopis_state(item({"isInStock": True, "quantity": 9}), "1754") == "active"
    assert bopis_state(item({"isLimitedQuantity": True, "isInStock": True}), "1754") == "limited"
    assert bopis_state(item({"isOutOfStock": True}), "1754") == "unavailable"
    assert bopis_state(item({"isInStock": True}, service="express"), "1754") == "none"
    assert bopis_state(item({"isInStock": True}, store="9999"), "1754") == "none"
    assert bopis_state({}, "1754") is None
    assert bopis_state({"fulfillment": None}, "1754") is None


def test_parser_attaches_state_and_stays_null_safe(sample_response):
    snaps = parse_snapshots(sample_response, "2619")
    assert snaps and all(hasattr(s, "bopis_state") for s in snaps)
    assert parse_snapshots({"data": None}, "1") == []


# ---- scoring ---------------------------------------------------------------


def test_neutral_with_no_evidence():
    c = score_stock(qty=5, in_stock=True, title="M18 drill", now=NOW)
    assert c.score == BASE_SCORE and c.label == "Medium" and c.tags == []


def test_out_of_stock_scores_zero():
    assert score_stock(qty=0, in_stock=False, now=NOW).score == 0


def test_live_pickup_boosts_limited_penalises_unavailable_does_not():
    live = score_stock(qty=5, in_stock=True, bopis_state="active", now=NOW)
    limited = score_stock(qty=5, in_stock=True, bopis_state="limited", now=NOW)
    off = score_stock(qty=5, in_stock=True, bopis_state="unavailable", now=NOW)
    assert live.score > BASE_SCORE > limited.score
    assert off.score == BASE_SCORE  # clearance is not sold online; off != ghost


def test_falling_stock_boosts():
    c = score_stock(qty=5, in_stock=True, history=readings((6, 12), (3, 8), (0, 5)), now=NOW)
    assert c.score > BASE_SCORE and any("falling" in t for t in c.tags)


def test_restock_is_not_falling():
    c = score_stock(qty=8, in_stock=True, history=readings((6, 5), (3, 12), (0, 8)), now=NOW)
    assert not any("falling" in t for t in c.tags)


def test_static_low_stock_for_ten_days_is_penalised():
    hist = readings((14, 2), (10.5, 2), (6, 2), (2, 2), (0, 2))
    c = score_stock(qty=2, in_stock=True, history=hist, now=NOW)
    assert c.score < BASE_SCORE and any("static-low" in t for t in c.tags)


def test_static_stock_seen_only_recently_is_not_penalised():
    hist = readings((2, 2), (1, 2), (0, 2))
    c = score_stock(qty=2, in_stock=True, history=hist, now=NOW)
    assert not any("static-low" in t for t in c.tags)


def test_high_static_stock_is_not_ghost_stock():
    hist = readings((14, 9), (8, 9), (4, 9), (0, 9))
    assert not any("static-low" in t for t in
                   score_stock(qty=9, in_stock=True, history=hist, now=NOW).tags)


def test_department_floors():
    assert classify_department("Pre-lit Christmas Tree") == "seasonal"
    assert classify_department("M18 Impact Driver") == "year_round"
    seasonal = score_stock(qty=3, in_stock=True, title="Patio dining set", now=NOW)
    assert seasonal.score < BASE_SCORE
    seasonal_ok = score_stock(qty=6, in_stock=True, title="Patio dining set", now=NOW)
    assert not any("needs" in t for t in seasonal_ok.tags)
    seasonal_pickup = score_stock(qty=2, in_stock=True, title="Patio dining set",
                                  bopis_state="active", now=NOW)
    assert not any("needs" in t for t in seasonal_pickup.tags)
    single = score_stock(qty=1, in_stock=True, title="Hammer", now=NOW)
    assert single.score < BASE_SCORE and any("single-unit" in t for t in single.tags)
    assert not any("single" in t for t in
                   score_stock(qty=2, in_stock=True, title="Hammer", now=NOW).tags)


def test_cluster_confirmation_needs_three_stores_with_four_units():
    c = score_stock(qty=5, in_stock=True, peer_qtys=[6, 4], stores_watched=3, now=NOW)
    assert c.score > BASE_SCORE and any("cluster" in t for t in c.tags)
    weak = score_stock(qty=5, in_stock=True, peer_qtys=[6, 2], stores_watched=3, now=NOW)
    assert not any("cluster" in t for t in weak.tags)
    two_stores = score_stock(qty=5, in_stock=True, peer_qtys=[6], stores_watched=2, now=NOW)
    assert not any("cluster" in t for t in two_stores.tags)


def test_isolated_flagged_only_when_enough_stores_are_watched():
    iso = score_stock(qty=5, in_stock=True, peer_qtys=[], stores_watched=3, now=NOW)
    assert any("isolated" in t for t in iso.tags)
    assert not any("isolated" in t for t in
                   score_stock(qty=5, in_stock=True, stores_watched=2, now=NOW).tags)


def test_score_is_clamped():
    c = score_stock(qty=20, in_stock=True, bopis_state="active",
                    history=readings((6, 30), (3, 25), (0, 20)),
                    peer_qtys=[9, 9], stores_watched=3, now=NOW)
    assert 0 <= c.score <= 100


def test_no_bs_passes_unscored_and_threshold():
    assert passes_no_bs(None, 40)
    low = score_stock(qty=1, in_stock=True, bopis_state="limited", now=NOW)
    assert not passes_no_bs(low, 40)
    assert passes_no_bs(score_stock(qty=5, in_stock=True, now=NOW), 40)


# ---- markdown cadence and price context --------------------------------------


@pytest.mark.parametrize("price,stage", [(19.00, 0), (19.06, 1), (19.03, 2), (19.02, 3), (19.01, 4)])
def test_markdown_ladder(price, stage):
    assert markdown_ending(price)["stage"] == stage


def test_off_ladder_and_none():
    assert markdown_ending(19.99)["stage"] is None
    assert markdown_ending(None) is None


def test_days_at_current_price_counts_from_first_reading_at_it():
    r = [(NOW - timedelta(days=9), 50.0), (NOW - timedelta(days=5), 30.03),
         (NOW - timedelta(days=2), 30.03), (NOW, 30.03)]
    assert days_at_current_price(r, NOW) == 5
    assert days_at_current_price([], NOW) is None


def test_median_and_inflated_discount():
    assert median_price([10, 30, 20, None]) == 20
    ctx = discount_context(current=50, msrp=200, typical=60, sample_days=40)
    assert ctx["claimed_pct"] == 75 and ctx["true_pct"] == 17 and ctx["inflated"]
    honest = discount_context(current=100, msrp=200, typical=200, sample_days=40)
    assert not honest["inflated"]
    assert discount_context(None, 1, 1)["inflated"] is False


# ---- probe candidates / digest ----------------------------------------------


def test_probe_picks_new_then_deepest_within_limit():
    board = {"1": [{"item_id": "a", "pct_off": 50}, {"item_id": "b", "pct_off": 70},
                   {"item_id": "c", "pct_off": 10, "is_new": True},
                   {"item_id": "d", "pct_off": 20}]}
    assert pick_candidates(board, 45, 2) == [("1", "c"), ("1", "b")]
    assert pick_candidates(board, 45, 0) == []


def test_digest_shows_confidence_and_inflation():
    row = {"alert_type": "IN_STORE_CLEARANCE", "store_id": "1754", "item_id": "9",
           "payload": {"after": {"clearance_value": 50, "price_value": 200, "inventory_qty": 4},
                       "confidence": {"score": 72, "label": "High", "tags": ["pickup-live +20"]},
                       "context": {"inflated": True, "claimed_pct": 75, "true_pct": 17,
                                   "typical_price": 60}}}
    line = format_alert(row)
    assert "stock confidence High 72" in line and "pickup-live" in line and "INFLATED" in line
