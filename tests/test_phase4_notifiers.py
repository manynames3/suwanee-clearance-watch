"""Phase 4: push notifiers, rule subscriptions and dedupe."""

import asyncio
import json

import pytest

from hd.config import Settings
from hd.notifiers import channels as ch
from hd.notifiers.base import Notification
from hd.notifiers.dedupe import SentLog
from hd.notifiers.dispatch import dispatch_alerts
from hd.notifiers.rules import Rule, parse_rules, select


def alert(item="100", store="1754", deal=40.0, was=100.0, conf=70, title="Milwaukee Drill", **extra):
    payload = {
        "after": {"clearance_value": deal, "price_original": was, "inventory_qty": 3},
        "confidence": {"score": conf, "label": "likely", "tags": ["bopis"]},
        "product_url": f"https://example.test/p/{item}",
        **extra,
    }
    return {"alert_type": "CLEARANCE_DETECTED", "store_id": store, "item_id": item,
            "payload": payload, "product_title": title}


def settings(tmp_path, **kw):
    return Settings(notify_dedupe_path=str(tmp_path / "sent.json"), **kw)


class Recorder:
    def __init__(self, name="rec", ok=True, enabled=True):
        self.name, self._ok, self._enabled, self.calls = name, ok, enabled, []

    def enabled(self, s):
        return self._enabled

    async def send(self, s, items):
        self.calls.append(items)
        return self._ok


# ── Notification ──────────────────────────────────────────────────────────────

def test_notification_math_and_text():
    n = Notification.from_alert(alert(), {"1754": "Suwanee"})
    assert (n.deal_price, n.was_price, n.percent_off) == (40.0, 100.0, 60)
    assert "60% off" in n.headline()
    assert "Suwanee" in n.body() and "verify in store" in n.body()


def test_notification_without_prices_is_safe():
    n = Notification.from_alert({"alert_type": "X", "store_id": 1, "item_id": 2, "payload": {}})
    assert n.deal_price is None and n.percent_off is None
    assert "price unknown" in n.headline()


# ── rules ─────────────────────────────────────────────────────────────────────

def test_no_rules_sends_everything():
    items = [Notification.from_alert(alert(item=str(i))) for i in range(3)]
    assert select(items, []) == items


def test_rule_thresholds_and_keywords():
    good = Notification.from_alert(alert(deal=30, conf=80))
    meh = Notification.from_alert(alert(item="2", deal=90, conf=80))
    low = Notification.from_alert(alert(item="3", deal=30, conf=20))
    rules = parse_rules('[{"min_percent_off": 50, "min_confidence": 60, "keywords": ["milwaukee"]}]')
    assert select([good, meh, low], rules) == [good]


def test_rules_are_any_of_and_store_scoped():
    a = Notification.from_alert(alert(store="1754", deal=95))
    b = Notification.from_alert(alert(item="2", store="6978", deal=10))
    rules = [Rule(stores=["6978"]), Rule(min_percent_off=90)]
    assert select([a, b], rules) == [b]


def test_unscored_deal_is_not_failed_by_confidence_floor():
    n = Notification.from_alert({"alert_type": "X", "store_id": "1", "item_id": "2",
                                 "payload": {"after": {"clearance_value": 5, "price_original": 10}}})
    assert Rule(min_confidence=90).matches(n)


@pytest.mark.parametrize("raw", ["not json", '"str"', '[{"min_percent_off": "lots"}]'])
def test_malformed_rules_fail_loudly(raw):
    with pytest.raises(ValueError):
        parse_rules(raw)


# ── dedupe ────────────────────────────────────────────────────────────────────

def test_sentlog_suppresses_within_window_and_expires(tmp_path):
    n = Notification.from_alert(alert())
    log = SentLog(tmp_path / "s.json", window_hours=1)
    assert log.unseen([n], now=0) == [n]
    log.mark([n], now=0)
    again = SentLog(tmp_path / "s.json", window_hours=1)  # persisted
    assert again.unseen([n], now=100) == []
    assert again.unseen([n], now=4000) == [n]


def test_sentlog_dedupes_within_a_batch_and_survives_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    n = Notification.from_alert(alert())
    assert SentLog(p).unseen([n, n]) == [n]


def test_price_change_is_a_new_notification(tmp_path):
    a, b = Notification.from_alert(alert(deal=40)), Notification.from_alert(alert(deal=30))
    log = SentLog(tmp_path / "s.json")
    log.mark([a])
    assert log.unseen([b]) == [b]


# ── dispatch ──────────────────────────────────────────────────────────────────

def test_dispatch_sends_once_then_dedupes(tmp_path):
    rec = Recorder()
    s = settings(tmp_path)
    r1 = asyncio.run(dispatch_alerts(s, [alert()], notifiers=[rec]))
    r2 = asyncio.run(dispatch_alerts(s, [alert()], notifiers=[rec]))
    assert r1.delivered == {"rec": True} and len(rec.calls) == 1
    assert r2.selected == 0


def test_failed_delivery_is_retried_next_scan(tmp_path):
    bad, s = Recorder(ok=False), settings(tmp_path)
    asyncio.run(dispatch_alerts(s, [alert()], notifiers=[bad]))
    good = Recorder()
    asyncio.run(dispatch_alerts(s, [alert()], notifiers=[good]))
    assert len(good.calls) == 1


def test_one_channel_failing_does_not_block_others(tmp_path):
    class Boom(Recorder):
        async def send(self, s, items):
            raise RuntimeError("down")

    ok = Recorder("ok")
    r = asyncio.run(dispatch_alerts(settings(tmp_path), [alert()], notifiers=[Boom("boom"), ok]))
    assert r.delivered == {"boom": False, "ok": True}


def test_health_alerts_are_never_pushed_as_deals(tmp_path):
    rec = Recorder()
    h = {**alert(), "alert_type": "HEALTH_DEGRADED"}
    r = asyncio.run(dispatch_alerts(settings(tmp_path), [h], notifiers=[rec]))
    assert r.considered == 0 and not rec.calls


def test_no_enabled_channel_is_a_noop(tmp_path):
    r = asyncio.run(dispatch_alerts(settings(tmp_path), [alert()], notifiers=[Recorder(enabled=False)]))
    assert r.considered == 0


# ── channels (HTTP stubbed) ───────────────────────────────────────────────────

@pytest.fixture
def posts(monkeypatch):
    seen = []

    async def fake(url, **kw):
        seen.append((url, kw))
        return 200

    monkeypatch.setattr(ch, "http_post", fake)
    return seen


def run(notifier, s, n=1):
    items = [Notification.from_alert(alert(item=str(i))) for i in range(n)]
    return asyncio.run(notifier.send(s, items))


def test_channels_are_off_without_credentials():
    s = Settings()
    assert not any(n.enabled(s) for n in ch.ALL_NOTIFIERS)


def test_ntfy_posts_to_topic(posts):
    s = Settings(ntfy_topic="deals")
    assert ch.NtfyNotifier().enabled(s) and run(ch.NtfyNotifier(), s)
    assert posts[0][0] == "https://ntfy.sh/deals"
    assert "Title" in posts[0][1]["headers"]


def test_pushover_telegram_discord_payloads(posts):
    assert run(ch.PushoverNotifier(), Settings(pushover_token="t", pushover_user="u"))
    assert posts[-1][1]["form"]["user"] == "u"
    assert run(ch.TelegramNotifier(), Settings(telegram_bot_token="B", telegram_chat_id="9"))
    assert posts[-1][0].endswith("/botB/sendMessage")
    assert run(ch.DiscordNotifier(), Settings(discord_webhook_url="https://discord.test/hook"))
    assert "clearance deal" in posts[-1][1]["json_body"]["content"]


def test_discord_rejects_non_https_webhook():
    assert not ch.DiscordNotifier().enabled(Settings(discord_webhook_url="http://x"))


def test_long_batches_are_truncated(posts):
    run(ch.NtfyNotifier(), Settings(ntfy_topic="d"), n=25)
    body = posts[0][1]["data"].decode()
    assert "and 15 more" in body


def test_http_failure_reports_false(monkeypatch):
    async def fail(url, **kw):
        return 500

    monkeypatch.setattr(ch, "http_post", fail)
    assert not run(ch.NtfyNotifier(), Settings(ntfy_topic="d"))


def test_email_sends_via_smtp(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, m):
            sent["subject"] = m["Subject"]

    monkeypatch.setattr(ch.smtplib, "SMTP", FakeSMTP)
    s = Settings(email_smtp_host="smtp.test", email_to="me@test", email_user="u", email_password="p")
    assert run(ch.EmailNotifier(), s)
    assert sent["tls"] and sent["login"] == "u" and "clearance deal" in sent["subject"]
