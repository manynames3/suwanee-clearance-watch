"""Phase 5: upstream schema validation and the drift canary."""

import copy
import json
from pathlib import Path

import pytest

from hd.hd_api.schema import MIN_PRODUCTS_TO_JUDGE, check_search_response

FIXTURE = Path(__file__).parent / "fixtures" / "sample_searchModel_response.json"


@pytest.fixture
def good():
    return json.loads(FIXTURE.read_text())


def widen(raw, n=12):
    """Repeat the fixture's products so the page is big enough to judge."""
    r = copy.deepcopy(raw)
    base = r["data"]["searchModel"]["products"]
    r["data"]["searchModel"]["products"] = [copy.deepcopy(base[i % len(base)]) for i in range(n)]
    return r


def test_recorded_response_matches_the_contract(good):
    rep = check_search_response(good)
    assert rep.ok and rep.products == len(good["data"]["searchModel"]["products"])


def test_full_page_reports_coverage(good):
    rep = check_search_response(widen(good))
    assert rep.ok and rep.coverage["itemId"] == 1.0 and rep.coverage["pricing.value"] == 1.0


@pytest.mark.parametrize("junk", [None, [], "x", 3])
def test_non_objects_are_reported_not_raised(junk):
    assert not check_search_response(junk).ok


def test_missing_search_model_is_drift():
    rep = check_search_response({"data": {}})
    assert not rep.ok and "searchModel" in rep.problems[0]


def test_wrong_container_type_is_drift(good):
    bad = widen(good)
    bad["data"]["searchModel"]["products"][0]["pricing"] = ["not", "an", "object"]
    rep = check_search_response(bad)
    assert not rep.ok and "pricing" in rep.problems[0]


def test_renamed_price_field_is_flagged_by_coverage(good):
    bad = widen(good)
    for p in bad["data"]["searchModel"]["products"]:
        p["pricing"]["cost"] = p["pricing"].pop("value")  # upstream renamed it
    rep = check_search_response(bad)
    assert not rep.ok and any("pricing.value" in m for m in rep.problems)


def test_dropped_fulfillment_is_flagged(good):
    bad = widen(good)
    for p in bad["data"]["searchModel"]["products"]:
        p.pop("fulfillment", None)
    assert not check_search_response(bad).ok


def test_sparse_optional_fields_do_not_alarm(good):
    ok = widen(good)
    for p in ok["data"]["searchModel"]["products"]:
        p["identifiers"].pop("upc", None)
    assert check_search_response(ok).ok


def test_small_pages_are_not_judged(good):
    tiny = copy.deepcopy(good)
    tiny["data"]["searchModel"]["products"] = [{"itemId": "1"}]
    assert len(tiny["data"]["searchModel"]["products"]) < MIN_PRODUCTS_TO_JUDGE
    assert check_search_response(tiny).ok


def test_extra_upstream_fields_are_tolerated(good):
    extra = widen(good)
    extra["data"]["searchModel"]["products"][0]["brandNewField"] = {"anything": 1}
    assert check_search_response(extra).ok


def test_canary_command_reports_ok_and_drift(monkeypatch, tmp_path, good):
    from typer.testing import CliRunner

    from hd.cli import app
    from hd.retailers.homedepot import HomeDepotAdapter

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STORES", "1754")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def fake(self, client, **kw):
        return fake.payload

    monkeypatch.setattr(HomeDepotAdapter, "browse_category", fake)
    monkeypatch.setattr("hd.http.client.HDClient.close", lambda self: _noop())

    fake.payload = widen(good)
    ok = CliRunner().invoke(app, ["canary"])
    assert ok.exit_code == 0 and "schema ok" in ok.output

    bad = widen(good)
    for p in bad["data"]["searchModel"]["products"]:
        p.pop("pricing", None)
    fake.payload = bad
    drift = CliRunner().invoke(app, ["canary"])
    assert drift.exit_code == 1 and "drift" in drift.output

    fake.payload = {"_hd_failure": "throttled"}
    blocked = CliRunner().invoke(app, ["canary"])
    assert blocked.exit_code == 3


async def _noop():
    return None
