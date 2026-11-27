"""Channel-neutral notification model and the Notifier protocol."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hd.config import Settings

HTTP_TIMEOUT_S = 10


@dataclass
class Notification:
    """One deal, reduced to what a phone notification needs."""

    key: str  # dedupe identity: store:item:price
    alert_type: str
    store_id: str
    store_name: str
    item_id: str
    title: str
    deal_price: float | None
    was_price: float | None
    percent_off: int | None
    confidence: int | None
    confidence_label: str
    qty: int | None
    url: str | None
    inflated: bool = False
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_alert(cls, row: dict[str, Any], store_names: dict[str, str] | None = None) -> Notification:
        p = row.get("payload") or {}
        after = p.get("after") or {}
        before = p.get("before") or {}
        clearance = after.get("clearance_value")
        deal = clearance if clearance is not None else after.get("price_value")
        refs = [
            r
            for r in (
                after.get("price_original"),
                before.get("price_value"),
                p.get("reference_price"),
                after.get("price_value") if clearance is not None else None,
            )
            if r is not None
        ]
        was = max(refs) if refs else None
        pct = None
        if deal is not None and was is not None and was > deal:
            pct = round((was - deal) / was * 100)
        conf = p.get("confidence") or {}
        ctx = p.get("context") or {}
        store_id = str(row.get("store_id"))
        item_id = str(row.get("item_id"))
        return cls(
            key=f"{store_id}:{item_id}:{deal}",
            alert_type=str(row.get("alert_type")),
            store_id=store_id,
            store_name=(store_names or {}).get(store_id, "") or str(row.get("store_name") or ""),
            item_id=item_id,
            title=str(row.get("product_title") or p.get("product_title") or "title unknown"),
            deal_price=float(deal) if deal is not None else None,
            was_price=float(was) if was is not None else None,
            percent_off=pct,
            confidence=conf.get("score"),
            confidence_label=str(conf.get("label") or ""),
            qty=after.get("inventory_qty"),
            url=p.get("product_url"),
            inflated=bool(ctx.get("inflated")),
            tags=list(conf.get("tags") or []),
        )

    def headline(self) -> str:
        price = f"${self.deal_price:,.2f}" if self.deal_price is not None else "price unknown"
        off = f" ({self.percent_off}% off)" if self.percent_off is not None else ""
        return f"{price}{off} · {self.title}"

    def body(self) -> str:
        store = f"#{self.store_id}" + (f" {self.store_name}" if self.store_name else "")
        bits = [f"Store {store}", f"item {self.item_id}"]
        if self.qty is not None:
            bits.append(f"{self.qty} in stock")
        if self.confidence is not None:
            bits.append(f"stock confidence {self.confidence_label} {self.confidence}".replace("  ", " "))
        if self.inflated:
            bits.append("discount claim looks inflated")
        bits.append("estimate only: verify in store")
        return " · ".join(bits)


@runtime_checkable
class Notifier(Protocol):
    name: str

    def enabled(self, settings: Settings) -> bool: ...

    async def send(self, settings: Settings, items: list[Notification]) -> bool: ...


async def http_post(
    url: str,
    *,
    json_body: dict | None = None,
    form: dict | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    """POST and return the HTTP status (0 on a network error). Stdlib only."""

    def _do() -> int:
        h = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            h.setdefault("Content-Type", "application/json")
        elif form is not None:
            from urllib.parse import urlencode

            body = urlencode(form).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data or b""
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0

    return await asyncio.to_thread(_do)
