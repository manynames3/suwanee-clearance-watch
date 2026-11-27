"""Phase 1: adaptive limiter, drip pacing, soft-block detection, tier gate."""

from __future__ import annotations

import asyncio
import json

import pytest

from hd.config import Settings
from hd.http.client import HDClient
from hd.http.rate_limit import TokenBucketRateLimiter
from hd.http.softblock import KNOWN_POPULATED_MIN, SoftBlockDetector, category_key
from hd.http.transport import RawResponse


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def slept(monkeypatch):
    durations: list[float] = []

    async def fake_sleep(seconds):
        durations.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return durations


def page(n_products: int, total: int) -> dict:
    return {
        "data": {
            "searchModel": {
                "products": [{"itemId": str(i)} for i in range(n_products)],
                "searchReport": {"totalProducts": total},
            }
        }
    }


def reply(body: dict, status: int = 200) -> RawResponse:
    return RawResponse(
        status=status, body=json.dumps(body), headers={"Content-Type": "application/json"}
    )


class Scripted:
    def __init__(self, responses):
        self.queue = list(responses)
        self.requests = 0

    async def post_json(self, url, payload, headers):
        self.requests += 1
        return self.queue.pop(0)

    async def close(self):
        pass


def make_client(responses, **overrides):
    base = dict(
        _env_file=None,
        rate_limit_rps=1000.0,
        jitter_min_ms=0,
        jitter_max_ms=0,
        request_budget=0,
        api_endpoint="https://api.example.invalid/graphql",
        drip_feed_enabled=False,
    )
    base.update(overrides)
    client = HDClient(Settings(**base), transport=Scripted(responses))
    client._query_cache = "query searchModel { x }"
    return client


VARS = {"navParam": "N-1", "storeId": "1754", "storefilter": "ALL", "startIndex": 0, "pageSize": 24}


# ---- adaptive limiter -------------------------------------------------------


def test_slow_down_halves_rate_and_respects_floor():
    lim = TokenBucketRateLimiter(rps=1.0, burst=1)
    assert lim.slow_down() == 0.5
    for _ in range(10):
        lim.slow_down()
    assert lim.effective_rps == pytest.approx(0.125)
    assert lim.is_slowed


def test_recovery_never_exceeds_configured_ceiling():
    lim = TokenBucketRateLimiter(rps=1.0, burst=1)
    lim.slow_down()
    for _ in range(1000):
        lim.note_success()
    assert lim.effective_rps == pytest.approx(1.0)
    assert not lim.is_slowed


def test_recovery_needs_a_clean_streak():
    lim = TokenBucketRateLimiter(rps=1.0, burst=1)
    lim.slow_down()
    for _ in range(9):
        lim.note_success()
    assert lim.effective_rps == 0.5
    lim.note_success()
    assert lim.effective_rps > 0.5


async def test_429_slows_the_limiter(slept):
    client = make_client([reply({}, 429), reply(page(1, 1))])
    await client.post_graphql(dict(VARS))
    assert client.rate_limiter.is_slowed


# ---- drip pacing ------------------------------------------------------------


async def test_drip_pace_waits_between_requests(slept):
    lim = TokenBucketRateLimiter(
        rps=1000.0, burst=5, jitter_min_ms=0, jitter_max_ms=0, pace_min_s=4.0, pace_max_s=10.0
    )
    await lim.acquire()  # first request is not delayed
    await lim.acquire()
    assert any(4.0 - 0.5 <= d <= 10.0 for d in slept), slept


async def test_idle_pause_every_n_requests(slept):
    lim = TokenBucketRateLimiter(
        rps=1000.0, burst=10, jitter_min_ms=0, jitter_max_ms=0,
        idle_every=3, idle_min_s=20.0, idle_max_s=60.0,
    )
    for _ in range(3):
        await lim.acquire()
    assert any(20.0 <= d <= 60.0 for d in slept), slept


# ---- soft block -------------------------------------------------------------


def test_detector_quiet_until_totals_are_learned():
    det = SoftBlockDetector(None)
    assert det.assess(dict(VARS), page(0, 0)) is None


def test_empty_first_page_on_known_populated_category_is_flagged():
    det = SoftBlockDetector(None)
    assert det.assess(dict(VARS), page(24, 300)) is None  # learns 300
    assert "first page empty" in det.assess(dict(VARS), page(0, 0))


def test_zero_total_with_products_missing_is_flagged():
    det = SoftBlockDetector(None)
    det.assess(dict(VARS), page(24, 300))
    assert det.assess(dict(VARS), page(0, 0)) is not None


def test_small_categories_may_legitimately_empty():
    det = SoftBlockDetector(None)
    det.assess(dict(VARS), page(5, KNOWN_POPULATED_MIN - 1))
    assert det.assess(dict(VARS), page(0, 0)) is None


def test_truncated_mid_walk_page_is_flagged_but_tail_is_not():
    det = SoftBlockDetector(None)
    det.assess(dict(VARS), page(24, 300))
    mid = {**VARS, "startIndex": 48}
    assert det.assess(mid, page(3, 300)) is not None
    tail = {**VARS, "startIndex": 288}
    assert det.assess(tail, page(3, 300)) is None


def test_genuine_shrinkage_is_learned_not_flagged():
    det = SoftBlockDetector(None)
    det.assess(dict(VARS), page(24, 300))
    assert det.assess(dict(VARS), page(24, 120)) is None
    assert det.known_total(dict(VARS)) == 120


def test_watchlist_keyword_searches_do_not_share_a_category_key():
    a = {"keyword": "111", "navParam": None, "storeId": "1", "storefilter": "ALL"}
    b = {"keyword": "222", "navParam": None, "storeId": "1", "storefilter": "ALL"}
    assert category_key(a) != category_key(b)


def test_known_totals_persist_and_corrupt_file_fails_open(tmp_path):
    path = tmp_path / "known.json"
    det = SoftBlockDetector(path)
    det.assess(dict(VARS), page(24, 300))
    det.save()
    assert SoftBlockDetector(path).known_total(dict(VARS)) == 300
    path.write_text("{not json")
    assert SoftBlockDetector(path).known_total(dict(VARS)) is None


async def test_client_stops_and_cools_down_on_soft_block(slept):
    client = make_client([reply(page(24, 300)), reply(page(0, 0)), reply(page(24, 300))])
    first = await client.post_graphql(dict(VARS))
    assert first["data"]["searchModel"]["products"]
    second = await client.post_graphql(dict(VARS))
    assert second.get("_hd_failure") == "soft_block"
    assert client.is_throttled
    assert client.cooldown.is_active()
    # nothing further is sent once the run has stopped
    third = await client.post_graphql(dict(VARS))
    assert third.get("_hd_failure") == "throttled"
    assert client._transport.requests == 2
    assert client.metrics.by_outcome().get("soft_block") == 1


async def test_soft_block_failure_is_not_a_valid_search_response(slept):
    from hd.hd_api.graphql import is_valid_search_response

    client = make_client([reply(page(24, 300)), reply(page(0, 0))])
    await client.post_graphql(dict(VARS))
    blocked = await client.post_graphql(dict(VARS))
    assert not is_valid_search_response(blocked)


# ---- tier gate --------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from hd.pipeline import tiering  # noqa: E402


def tier_settings(tmp_path, **kw):
    return Settings(_env_file=None, tier_state_path=str(tmp_path / "tier.json"), **kw)


def test_cold_tier_due_when_never_run(tmp_path):
    s = tier_settings(tmp_path)
    assert tiering.gate_tiers(s, ("shelf", "network")) == ("shelf", "network")


def test_cold_tier_skipped_inside_interval_shelf_untouched(tmp_path):
    s = tier_settings(tmp_path, network_tier_min_interval_hours=24.0)
    now = datetime.now(timezone.utc)
    tiering.mark_cold_completed(s, now - timedelta(hours=3))
    assert tiering.gate_tiers(s, ("shelf", "network"), now) == ("shelf",)
    assert tiering.gate_tiers(s, ("network",), now) == ()


def test_cold_tier_due_after_interval(tmp_path):
    s = tier_settings(tmp_path, network_tier_min_interval_hours=24.0)
    now = datetime.now(timezone.utc)
    tiering.mark_cold_completed(s, now - timedelta(hours=25))
    assert tiering.gate_tiers(s, ("network",), now) == ("network",)


def test_gate_disabled_and_corrupt_state_fail_open(tmp_path):
    s = tier_settings(tmp_path, tiered_polling=False)
    tiering.mark_cold_completed(s)
    assert tiering.gate_tiers(s, ("network",)) == ("network",)
    s2 = tier_settings(tmp_path)
    (tmp_path / "tier.json").write_text("garbage")
    assert tiering.gate_tiers(s2, ("network",)) == ("network",)


# ---- network doctor ---------------------------------------------------------

from hd import network_doctor  # noqa: E402


def test_classify_prefers_the_worst_cause():
    assert network_doctor.classify({"ok": 50}) is None
    assert network_doctor.classify({"http_206_quota": 1, "ok": 9}) == "http_206_quota"
    assert network_doctor.classify({"http_206_quota": 3, "http_403": 1}) == "http_403"
    assert network_doctor.classify({"soft_block": 1, "http_429": 2}) == "soft_block"


def test_recent_outcomes_sum_last_runs_and_skip_bad_lines(tmp_path):
    f = tmp_path / "m.jsonl"
    f.write_text(
        json.dumps({"by_outcome": {"ok": 5}}) + "\nnot json\n"
        + json.dumps({"by_outcome": {"ok": 2, "http_429": 1}}) + "\n"
    )
    assert network_doctor.recent_outcomes(f) == {"ok": 7, "http_429": 1}
    assert network_doctor.recent_outcomes(tmp_path / "missing") == {}


def test_network_check_names_quota_and_never_suggests_evasion(tmp_path):
    f = tmp_path / "m.jsonl"
    f.write_text(json.dumps({"by_outcome": {"http_206_quota": 2, "ok": 3}}) + "\n")
    s = Settings(_env_file=None, metrics_path=str(f),
                 throttle_cooldown_path=str(tmp_path / "cd"))
    results = network_doctor.check_network(s)
    recent = next(c for c in results if c.name == "recent-runs")
    assert "QUOTA" in recent.detail and recent.status == "warn"
    text = " ".join(c.detail + (c.fix or "") for c in results).lower()
    for banned in ("proxy", "rotate", "spoof", "impersonat", "fingerprint"):
        assert banned not in text


def test_network_check_refusal_is_a_failure(tmp_path):
    f = tmp_path / "m.jsonl"
    f.write_text(json.dumps({"by_outcome": {"http_403": 1}}) + "\n")
    s = Settings(_env_file=None, metrics_path=str(f),
                 throttle_cooldown_path=str(tmp_path / "cd"))
    recent = next(c for c in network_doctor.check_network(s) if c.name == "recent-runs")
    assert recent.failed
