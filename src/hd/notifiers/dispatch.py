"""Fan alerts out to every configured channel, once each."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hd.config import Settings
from hd.logging import get_logger
from hd.notifiers.base import Notification, Notifier
from hd.notifiers.channels import ALL_NOTIFIERS
from hd.notifiers.dedupe import SentLog
from hd.notifiers.rules import parse_rules, select

log = get_logger("notifiers.dispatch")

_DEAL_TYPES_EXCLUDED = ("HEALTH_DEGRADED", "HEALTH_RECOVERED")


@dataclass
class DispatchResult:
    considered: int = 0
    selected: int = 0
    delivered: dict[str, bool] = field(default_factory=dict)

    @property
    def any_delivered(self) -> bool:
        return any(self.delivered.values())


async def dispatch_alerts(
    settings: Settings,
    alerts: list[dict[str, Any]],
    *,
    store_names: dict[str, str] | None = None,
    notifiers: list[Notifier] | None = None,
) -> DispatchResult:
    """Apply rules and dedupe, then send one batch per enabled channel.

    An item is marked sent only after at least one channel accepted it, so a
    delivery failure retries on the next scan instead of losing the deal.
    """
    result = DispatchResult()
    candidates: list[Notifier] = notifiers if notifiers is not None else list(ALL_NOTIFIERS)
    channels = [n for n in candidates if n.enabled(settings)]
    if not channels:
        return result

    items = [
        Notification.from_alert(a, store_names)
        for a in alerts
        if a.get("alert_type") not in _DEAL_TYPES_EXCLUDED
    ]
    result.considered = len(items)
    items = select(items, parse_rules(settings.notify_rules))
    sent_log = SentLog(settings.notify_dedupe_path, settings.notify_dedupe_hours)
    items = sent_log.unseen(items)
    result.selected = len(items)
    if not items:
        return result

    for ch in channels:
        try:
            result.delivered[ch.name] = bool(await ch.send(settings, items))
        except Exception as exc:  # a channel must never break the scan
            log.warning("Notifier failed", channel=ch.name, error=str(exc))
            result.delivered[ch.name] = False
    if result.any_delivered:
        sent_log.mark(items)
    return result
