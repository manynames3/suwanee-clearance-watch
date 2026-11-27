"""Rule-based subscriptions: which deals are worth a buzz.

NOTIFY_RULES is a JSON list. A deal is sent when it matches ANY rule; with no
rules configured, every deal that survived the No-BS filter is sent.

    [{"min_percent_off": 50, "min_confidence": 60},
     {"keywords": ["milwaukee", "dewalt"], "min_percent_off": 30, "stores": ["1754"]}]
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import BaseModel, ValidationError

from hd.notifiers.base import Notification


class Rule(BaseModel):
    min_percent_off: int = 0
    min_confidence: int = 0
    max_price: float | None = None
    stores: list[str] = []
    keywords: list[str] = []
    alert_types: list[str] = []
    exclude_inflated: bool = False

    def matches(self, n: Notification) -> bool:
        if (n.percent_off or 0) < self.min_percent_off:
            return False
        # An unscored deal is not held to a confidence floor: absence of a
        # score is "unknown", not "bad".
        if n.confidence is not None and n.confidence < self.min_confidence:
            return False
        if self.max_price is not None and (n.deal_price is None or n.deal_price > self.max_price):
            return False
        if self.stores and n.store_id not in self.stores:
            return False
        if self.alert_types and n.alert_type not in self.alert_types:
            return False
        if self.keywords:
            hay = n.title.lower()
            if not any(k.lower() in hay for k in self.keywords):
                return False
        if self.exclude_inflated and n.inflated:
            return False
        return True


def parse_rules(raw: str) -> list[Rule]:
    """Parse NOTIFY_RULES. Raises ValueError on malformed input.

    Bad rules must be loud: silently dropping a rule would silently widen or
    empty what the user gets notified about.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"NOTIFY_RULES is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("NOTIFY_RULES must be a JSON list of rule objects")
    try:
        return [Rule.model_validate(d) for d in data]
    except ValidationError as exc:
        raise ValueError(f"NOTIFY_RULES has an invalid rule: {exc}") from exc


def select(items: Iterable[Notification], rules: list[Rule]) -> list[Notification]:
    items = list(items)
    if not rules:
        return items
    return [n for n in items if any(r.matches(n) for r in rules)]
