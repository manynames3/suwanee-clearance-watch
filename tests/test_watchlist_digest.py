"""Watchlist, dollar floor and Markdown digest.

The API responses here are constructed, not captured: they follow the shape of
tests/fixtures/sample_searchModel_response.json. The item, prices and store are
the operator's acceptance case, used to prove the path from a response to a
digest line — not to prove what Home Depot returns for that item today.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hd.config import Settings
from hd.db.models import Alert, AlertType, Severity
from hd.pipeline.alerts import apply_dollar_floor, dollar_saving, write_alerts
from hd.pipeline.diff import run_diff
from hd.pipeline.digest import (
    RunReport,
    digest_path,
    format_alert,
    previously_seen,
    render_section,
    write_digest,
)
from hd.pipeline.watchlist import read_watchlist, request_allowance, run_watchlist

PRIMER = "203317264"


def _response(item_id: str, store_id: str, *, clearance: float | None = 5.30, qty: int = 1) -> dict:
    return {
        "data": {"searchModel": {
            "searchReport": {"totalProducts": 1},
            "products": [{
                "itemId": item_id,
                "identifiers": {
                    "brandName": "Zinsser", "modelNumber": "60001",
                    "productLabel": "Peel Stop 1 gal. White Triple-Thick Primer",
                    "canonicalUrl": f"/p/{item_id}",
                },
                "pricing": {
                    "value": 53.98, "original": 53.98, "promotion": None,
                    "clearance": None if clearance is None else {
                        "value": clearance,
                        "dollarOff": round(53.98 - clearance, 2),
                        "percentageOff": 90,
                    },
                    "specialBuy": None,
                },
                "media": {"images": []},
                "fulfillment": {"fulfillmentOptions": [{"type": "pickup", "services": [{
                    "type": "bopis",
                    "locations": [{
                        "locationId": store_id,
                        "inventory": {"quantity": qty, "isInStock": qty > 0,
                                      "isOutOfStock": qty == 0},
                    }],
                }]}]},
            }],
        }}
    }


class FakeClient:
    """Answers item lookups; goes throttled after `throttle_after` requests."""

    def __init__(self, *, throttle_after: int | None = None, empty: set[str] | None = None,
                 clearance: float | None = 5.30):
        self.requested: list[tuple[str, str]] = []
        self.throttle_after = throttle_after
        self.empty = empty or set()
        self.clearance = clearance

    async def post_graphql(self, variables):
        item_id, store_id = variables["keyword"], variables["storeId"]
        self.requested.append((store_id, item_id))
        if item_id in self.empty:
            return {"data": {"searchModel": {"searchReport": {"totalProducts": 0}, "products": []}}}
        return _response(item_id, store_id, clearance=self.clearance)

    @property
    def is_throttled(self):
        return self.throttle_after is not None and len(self.requested) >= self.throttle_after

    @property
    def request_count(self):
        return len(self.requested)

    async def close(self):
        pass


@pytest.fixture
def wl_settings(tmp_path) -> Settings:
    (tmp_path / "watchlist.txt").write_text(f"# finds\n{PRIMER}  # primer\n\n{PRIMER}\n")
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/wl.db",
        stores="1754,6978",
        brands="Milwaukee",
        watchlist_path=str(tmp_path / "watchlist.txt"),
        digest_dir=str(tmp_path / "digests"),
        store_raw_json=False,
    )


@pytest.fixture
async def fresh_db(wl_settings):
    from hd.db import base

    db = base.Database()
    with patch.object(base, "_default", db):
        await base.init_db(wl_settings)
        yield db
    await db.close_db()


async def _scan(settings, client, started=None) -> tuple[int, RunReport]:
    """Watchlist -> diff -> alerts, the way run-once chains them."""
    started = started or datetime.now(timezone.utc)
    await run_watchlist(settings, client, settings.store_list, now=started)
    alerts = await run_diff(settings)
    written = await write_alerts(settings, alerts)
    return written, RunReport(started=started, mode="browse", stores=settings.store_list)


class TestReadWatchlist:
    def test_comments_blanks_and_duplicates(self, wl_settings):
        ids, invalid = read_watchlist(wl_settings.watchlist_path)
        assert ids == [PRIMER]
        assert invalid == []

    def test_non_ids_are_reported_not_dropped_silently(self, tmp_path):
        f = tmp_path / "w.txt"
        f.write_text("123\nhttps://example.com/p/456\n")
        assert read_watchlist(f) == (["123"], ["https://example.com/p/456"])

    def test_missing_file_is_an_empty_list(self, tmp_path):
        assert read_watchlist(tmp_path / "nope.txt") == ([], [])

    def test_allowance_is_items_times_stores(self, wl_settings):
        assert request_allowance(wl_settings) == 2


class TestRunWatchlist:
    async def test_prices_each_item_at_each_store(self, wl_settings, fresh_db):
        client = FakeClient()
        summary = await run_watchlist(wl_settings, client, wl_settings.store_list)
        assert client.requested == [("1754", PRIMER), ("6978", PRIMER)]
        assert summary.snapshots == 2
        assert summary.complete

    async def test_throttle_marks_the_rest_as_unseen(self, wl_settings, fresh_db):
        client = FakeClient(throttle_after=1)
        summary = await run_watchlist(wl_settings, client, wl_settings.store_list)
        assert summary.aborted
        assert summary.failed == [("6978", PRIMER)]
        assert not summary.complete

    async def test_empty_answer_is_recorded_as_not_returned(self, wl_settings, fresh_db):
        summary = await run_watchlist(
            wl_settings, FakeClient(empty={PRIMER}), wl_settings.store_list
        )
        assert summary.snapshots == 0
        assert summary.not_returned == [("1754", PRIMER), ("6978", PRIMER)]

    async def test_cap_is_reported(self, wl_settings, fresh_db, tmp_path):
        (tmp_path / "watchlist.txt").write_text("1\n2\n3\n")
        capped = wl_settings.model_copy(update={"watchlist_max_items": 2})
        summary = await run_watchlist(capped, FakeClient(), ["1754"])
        assert summary.capped == 1
        assert not summary.complete


class TestAcceptance:
    async def test_first_scan_surfaces_the_primer_in_the_digest(self, wl_settings, fresh_db):
        written, report = await _scan(wl_settings, FakeClient())
        assert written >= 1
        path = await write_digest(wl_settings, report)
        text = path.read_text()
        line = next(
            ln for ln in text.splitlines()
            if "IN_STORE_CLEARANCE" in ln and "store #1754" in ln
        )
        assert PRIMER in line
        assert "**$5.30** (was $53.98, 90% off, saves $48.68)" in line
        assert "1 in stock" in line
        assert "retrieved 20" in line
        assert "HEALTH_DEGRADED" not in text

    async def test_second_run_with_no_changes_writes_no_alerts(self, wl_settings, fresh_db):
        await _scan(wl_settings, FakeClient())
        written, report = await _scan(wl_settings, FakeClient())
        assert written == 0
        text = (await write_digest(wl_settings, report)).read_text()
        assert "No changes since the previous run." in text

    async def test_partial_run_says_degraded_never_all_clear(self, wl_settings, fresh_db):
        await _scan(wl_settings, FakeClient())
        started = datetime.now(timezone.utc)
        client = FakeClient(throttle_after=1)
        summary = await run_watchlist(wl_settings, client, wl_settings.store_list, now=started)
        report = RunReport(started=started, mode="browse", stores=wl_settings.store_list)
        report.degraded.append(f"{len(summary.failed)} watchlist lookup(s) failed")
        text = render_section(report, [])
        assert "HEALTH_DEGRADED" in text
        assert "does not mean there are no deals" in text
        assert "No changes since the previous run." not in text

    async def test_digest_appends_one_section_per_run(self, wl_settings, fresh_db):
        _, first = await _scan(wl_settings, FakeClient())
        await write_digest(wl_settings, first)
        _, second = await _scan(wl_settings, FakeClient())
        path = await write_digest(wl_settings, second)
        text = path.read_text()
        assert text.count("## Run at ") == 2
        assert text.count("# Clearance digest") == 1
        assert path == digest_path(wl_settings, second.started)

    async def test_deferred_run_writes_a_digest_without_a_database(self, wl_settings):
        report = RunReport(
            started=datetime.now(timezone.utc), mode="deferred", has_alerts=False,
            degraded=["Run deferred without reading anything."],
        )
        text = (await write_digest(wl_settings, report)).read_text()
        assert "HEALTH_DEGRADED" in text

    async def test_previously_seen_separates_lost_items_from_wrong_ids(self, wl_settings, fresh_db):
        await _scan(wl_settings, FakeClient())
        seen = await previously_seen(wl_settings, [("1754", PRIMER), ("1754", "999")])
        assert seen == {("1754", PRIMER)}


def _alert(kind: AlertType, after: dict, **extra) -> Alert:
    return Alert(
        ts=datetime.now(timezone.utc), store_id="1754", item_id="1",
        alert_type=kind, severity=Severity.HIGH, payload={"after": after, **extra},
    )


class TestDollarFloor:
    def test_saving_uses_clearance_price_against_the_reference(self):
        a = _alert(AlertType.IN_STORE_CLEARANCE,
                   {"price_value": 53.98, "price_original": 53.98, "clearance_value": 5.30})
        assert dollar_saving(a) == 48.68

    def test_small_savings_are_dropped_large_kept(self):
        cheap = _alert(AlertType.CLEARANCE, {"price_value": 0.50, "price_original": 5.00})
        big = _alert(AlertType.CLEARANCE, {"price_value": 50.0, "price_original": 150.0})
        assert apply_dollar_floor([cheap, big], 20) == [big]

    def test_zero_floor_keeps_everything(self):
        cheap = _alert(AlertType.CLEARANCE, {"price_value": 0.50, "price_original": 5.00})
        assert apply_dollar_floor([cheap], 0) == [cheap]

    def test_unknown_saving_is_kept(self):
        a = _alert(AlertType.CLEARANCE, {"price_value": 9.0})
        assert dollar_saving(a) is None
        assert apply_dollar_floor([a], 20) == [a]

    def test_stock_alerts_are_never_floored(self):
        a = _alert(AlertType.BACK_IN_STOCK, {"price_value": 1.0, "price_original": 2.0})
        assert apply_dollar_floor([a], 20) == [a]

    async def test_floor_applies_when_alerts_are_written(self, wl_settings, fresh_db):
        floored = wl_settings.model_copy(update={"min_dollar_off": 100.0})
        await run_watchlist(floored, FakeClient(), floored.store_list)
        assert await write_alerts(floored, await run_diff(floored)) == 0


class TestFormatAlert:
    def test_missing_fields_are_written_as_unknown(self):
        line = format_alert({
            "alert_type": "CLEARANCE", "store_id": "1754", "item_id": "1",
            "payload": {"after": {"price_value": 9.0}},
        })
        assert "was-price unknown" in line
        assert "stock unknown" in line
        assert "title unknown" in line
        assert "retrieved unknown" in line

    def test_no_price_is_never_invented(self):
        line = format_alert({
            "alert_type": "BACK_IN_STOCK", "store_id": "1754", "item_id": "1",
            "payload": {"after": {"in_stock": True}},
        })
        assert "price unknown" in line
        assert "$" not in line


class TestRunOnceWiring:
    """`hd run-once` end to end, with the network and the brand walk faked."""

    @pytest.fixture
    def cli_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "watchlist.txt").write_text(f"{PRIMER}\n")
        env = {
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/cli.db",
            "STORES": "1754,6978", "BRANDS": "Milwaukee", "BRAND_TOKENS": "Milwaukee:zv",
            "WATCHLIST_PATH": str(tmp_path / "watchlist.txt"),
            "DIGEST_DIR": str(tmp_path / "digests"),
            "DAILY_DEALS_ENABLED": "false", "STORE_RAW_JSON": "false",
            "RATE_LIMIT_RPS": "1000", "JITTER_MIN_MS": "0", "JITTER_MAX_MS": "0",
            "THROTTLE_COOLDOWN_PATH": str(tmp_path / "cooldown"),
            "HEALTH_STATE_PATH": str(tmp_path / "health"),
            "METRICS_PATH": str(tmp_path / "metrics.jsonl"),
            "SLACK_BOT_TOKEN": "",
        }
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return tmp_path

    def _invoke(self, *, truncated=()):
        from typer.testing import CliRunner

        from hd.cli import app
        from hd.http.client import HDClient
        from hd.pipeline.browse import BrowseSummary

        fake = FakeClient()

        async def post(self, variables):
            self._request_count += 1
            return await fake.post_graphql(variables)

        async def browse(**kwargs):
            return BrowseSummary(truncated_walks=list(truncated))

        with patch.object(HDClient, "post_graphql", post), \
                patch("hd.pipeline.browse.run_browse", browse):
            return CliRunner().invoke(app, ["run-once"])

    def _digest(self, tmp_path) -> str:
        files = list((tmp_path / "digests").glob("clearance-digest-*.md"))
        assert len(files) == 1
        return files[0].read_text()

    def test_clean_run_lists_the_primer_then_goes_quiet(self, cli_env):
        result = self._invoke()
        assert result.exit_code == 0, result.output
        text = self._digest(cli_env)
        assert "**$5.30** (was $53.98, 90% off, saves $48.68)" in text
        assert "HEALTH_DEGRADED" not in text

        result = self._invoke()
        assert result.exit_code == 0, result.output
        second = self._digest(cli_env).split("## Run at ")[2]
        assert "No changes since the previous run." in second

    def test_truncated_walk_is_degraded_in_digest_and_terminal(self, cli_env):
        result = self._invoke(truncated=["Power Tools"])
        assert result.exit_code == 0, result.output
        assert "HEALTH_DEGRADED" in result.output
        text = self._digest(cli_env)
        assert "HEALTH_DEGRADED" in text
        assert "Power Tools" in text

    def test_deferred_run_still_writes_a_degraded_digest(self, cli_env):
        from hd.config import Settings as S
        from hd.http.cooldown import ThrottleCooldown

        s = S()
        ThrottleCooldown(s.throttle_cooldown_path, s.throttle_cooldown_seconds).start()
        result = self._invoke()
        assert result.exit_code == 0, result.output
        text = self._digest(cli_env)
        assert "HEALTH_DEGRADED" in text
        assert "Run deferred" in text


class TestDealsPage:
    async def test_site_data_carries_recorded_values_and_health(self, wl_settings, fresh_db):
        from hd.pipeline.site import build_site_data

        _, report = await _scan(wl_settings, FakeClient())
        report.degraded.append("Home Depot throttled this run (HTTP 206).")
        data = await build_site_data(wl_settings, report)
        assert [s["id"] for s in data["stores"]] == ["1754", "6978"]
        deal = data["stores"][0]["deals"][0]
        assert deal["item_id"] == PRIMER
        assert (deal["price"], deal["was"], deal["pct_off"], deal["saves"]) == (5.3, 53.98, 90, 48.68)
        assert deal["qty"] == 1
        assert deal["retrieved_at"].endswith("+00:00")
        assert data["run"]["degraded"] == ["Home Depot throttled this run (HTTP 206)."]

    async def test_dollar_floor_hides_small_savings_on_the_page(self, wl_settings, fresh_db):
        from hd.pipeline.site import build_site_data

        await _scan(wl_settings, FakeClient())
        floored = wl_settings.model_copy(update={"min_dollar_off": 100.0})
        data = await build_site_data(floored, None)
        assert all(s["deals"] == [] for s in data["stores"])
        assert data["run"]["started"] is None

    async def test_write_site_makes_a_complete_folder_with_no_secrets(self, wl_settings, fresh_db, tmp_path):
        import json as _json

        from hd.pipeline.site import build_site_data, write_site

        _, report = await _scan(wl_settings, FakeClient())
        s = wl_settings.model_copy(update={
            "site_out_dir": str(tmp_path / "out"), "slack_bot_token": "xoxb-secret",
        })
        out = write_site(s, await build_site_data(s, report))
        assert {p.name for p in out.iterdir()} == {"index.html", "deals.json", "_headers"}
        blob = (out / "deals.json").read_text()
        assert _json.loads(blob)["stores"][0]["deals"]
        assert "xoxb-secret" not in blob
        assert "noindex" in (out / "_headers").read_text()

    async def test_without_a_project_nothing_is_uploaded(self, wl_settings, fresh_db, tmp_path):
        from hd.pipeline import site

        s = wl_settings.model_copy(update={"site_out_dir": str(tmp_path / "out")})
        with patch.object(site, "deploy_site") as deploy:
            ok, message = await site.publish_site(s, None)
        assert ok and "not uploaded" in message
        deploy.assert_not_called()

    async def test_missing_npx_is_reported_not_raised(self, wl_settings, tmp_path):
        from hd.pipeline import site

        s = wl_settings.model_copy(update={"site_worker": "x"})
        with patch.object(site, "_find_npx", return_value=None):
            ok, message = await site.deploy_site(s, tmp_path)
        assert not ok and "npx" in message


class TestRunOncePublishes(TestRunOnceWiring):
    def test_run_uploads_the_page_and_survives_a_failed_upload(self, cli_env, monkeypatch):
        from hd.pipeline import site

        monkeypatch.setenv("SITE_WORKER", "demo")
        monkeypatch.setenv("SITE_OUT_DIR", str(cli_env / "out"))
        calls = []

        async def deploy(settings, out):
            calls.append(out)
            return False, "wrangler exploded"

        with patch.object(site, "deploy_site", deploy):
            result = self._invoke()
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert "could not be published" in result.output
        assert (cli_env / "out" / "deals.json").exists()
