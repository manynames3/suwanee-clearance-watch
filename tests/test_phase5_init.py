"""Phase 5: ZIP-agnostic init."""

import asyncio

import pytest
from typer.testing import CliRunner

from hd.hd_api.stores import InvalidZipCode, StoreResult
from hd.init_flow import choose_stores, run_init, store_spec


def st(i, dist, complete=True, name="Store"):
    return StoreResult(store_id=str(i), name=name, city="Town" if complete else None,
                       state="XX", zip="12345", distance_miles=dist)


def test_store_spec_round_trips_through_quick_setup_parser():
    from hd.quick_setup import parse_store

    s = parse_store(store_spec(st(1754, 1.0, name="Some|Pipe")))
    assert s.store_id == "1754" and "|" not in (s.name or "")


def test_store_spec_short_form_when_details_missing():
    s = StoreResult(store_id="9", name=None, city=None, state=None, zip=None)
    assert store_spec(s) == "9"


def test_choose_prefers_nearest_complete_stores():
    found = [st(1, 9.0), st(2, 2.0, complete=False), st(3, 4.0), st(4, 6.0)]
    assert [s.store_id for s in choose_stores(found, 2)] == ["3", "4"]


def test_choose_fills_with_incomplete_when_needed():
    found = [st(1, 9.0), st(2, 2.0, complete=False)]
    assert [s.store_id for s in choose_stores(found, 2)] == ["1", "2"]


def test_choose_rejects_zero():
    with pytest.raises(ValueError):
        choose_stores([st(1, 1.0)], 0)


def fake_search(stores):
    async def _s(zip_code, **kw):
        _s.kw = kw
        return stores

    return _s


def test_dry_run_writes_nothing(tmp_path):
    s = fake_search([st(1, 1.0)])
    r = asyncio.run(run_init(tmp_path, "30024", dry_run=True, searcher=s))
    assert r.chosen and r.setup is None and not (tmp_path / ".env").exists()
    assert s.kw["radius_miles"] == 25.0


def test_no_stores_found_configures_nothing(tmp_path):
    r = asyncio.run(run_init(tmp_path, "99999", searcher=fake_search([])))
    assert r.chosen == [] and r.setup is None


def test_lookup_errors_propagate(tmp_path):
    async def bad(zip_code, **kw):
        raise InvalidZipCode("nope")

    with pytest.raises(InvalidZipCode):
        asyncio.run(run_init(tmp_path, "abc", searcher=bad))


def test_init_wires_chosen_stores_into_quick_setup(tmp_path, monkeypatch):
    seen = {}

    async def fake_quick(root, specs, brands, **kw):
        seen.update(specs=specs, brands=brands, kw=kw)
        return "ok"

    monkeypatch.setattr("hd.init_flow.run_quick_setup", fake_quick)
    r = asyncio.run(run_init(tmp_path, "30024", max_stores=1, brands=["Ryobi"],
                             install_schedule=False, searcher=fake_search([st(7, 3.0), st(8, 1.0)])))
    assert seen["specs"][0].startswith("8|") and seen["brands"] == ["Ryobi"]
    assert seen["kw"]["install_schedule"] is False and r.setup == "ok"


def test_cli_dry_run_lists_stores(monkeypatch, tmp_path):
    from hd.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hd.hd_api.stores.search_stores", fake_search([st(5, 1.5), st(6, 3.0)]))
    out = CliRunner().invoke(app, ["init", "--zip", "30024", "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "5" in out.output and "Dry run" in out.output


def test_cli_reports_bad_zip(monkeypatch, tmp_path):
    from hd.cli import app

    async def bad(zip_code, **kw):
        raise InvalidZipCode("x")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hd.hd_api.stores.search_stores", bad)
    out = CliRunner().invoke(app, ["init", "--zip", "abc"])
    assert out.exit_code == 2 and "not a ZIP" in out.output


def test_container_files_are_consistent():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    docker = (root / "Dockerfile").read_text()
    assert "curl" in docker and "WORKDIR /data" in docker and "scan-loop" in docker
    loop = (root / "docker" / "scan-loop.sh").read_text()
    assert "hd run-once" in loop and "INTERVAL_HOURS" in loop
    compose = (root / "docker-compose.yml").read_text()
    assert "0.0.0.0" in compose  # the dashboard must bind outside the container
    assert (root / ".dockerignore").read_text().count(".env") >= 1  # secrets stay out of the image
