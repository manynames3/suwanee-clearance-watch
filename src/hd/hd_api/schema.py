"""Typed contract for the upstream searchModel response, and a drift report.

Home Depot's GraphQL shape is not ours and can change without notice. The
parsers are written to survive that (a missing field is skipped, not fatal),
which is also what makes a change silent: the scan "succeeds" while a field
quietly goes empty. This module names the fields the scanner relies on, checks
a response against them with pydantic, and reports what drifted, so a changed
shape is a loud, specific message instead of a slow rot in the data.

The models are deliberately lenient. Extra fields are allowed, every field is
optional, and numbers are not forced to a type; what is checked is the
*structure* the parsers walk (objects where objects are expected, lists where
lists are expected) and how many products carry each field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Identifiers(_Loose):
    brandName: str | None = None
    productLabel: str | None = None
    canonicalUrl: str | None = None
    modelNumber: str | None = None
    upc: str | None = None


class Image(_Loose):
    url: str | None = None


class Media(_Loose):
    images: list[Image] | None = None


class Clearance(_Loose):
    value: Any = None


class Pricing(_Loose):
    value: Any = None
    original: Any = None
    clearance: Clearance | None = None
    promotion: dict[str, Any] | None = None


class Inventory(_Loose):
    quantity: Any = None
    isInStock: bool | None = None
    isOutOfStock: bool | None = None


class Location(_Loose):
    locationId: Any = None
    inventory: Inventory | None = None


class Service(_Loose):
    type: str | None = None
    locations: list[Location] | None = None


class FulfillmentOption(_Loose):
    services: list[Service] | None = None


class Fulfillment(_Loose):
    fulfillmentOptions: list[FulfillmentOption] | None = None


class Product(_Loose):
    itemId: str | None = None
    identifiers: Identifiers | None = None
    media: Media | None = None
    pricing: Pricing | None = None
    fulfillment: Fulfillment | None = None


class SearchModel(_Loose):
    products: list[Product | None] | None = None


class SearchData(_Loose):
    searchModel: SearchModel | None = None


class SearchResponse(_Loose):
    data: SearchData | None = None


# The fields a product must carry for the scanner to do its job, with the
# share of products that should have each before it is called drift. UPC and
# clearance are legitimately sparse, so they are tracked but never alarm.
_REQUIRED_SHARE: dict[str, float] = {
    "itemId": 0.95,
    "identifiers.productLabel": 0.9,
    "pricing.value": 0.8,
    "fulfillment.fulfillmentOptions": 0.5,
}
_TRACKED_ONLY = ("identifiers.upc", "identifiers.brandName", "pricing.clearance", "media.images")
MIN_PRODUCTS_TO_JUDGE = 5


@dataclass
class SchemaReport:
    ok: bool
    products: int = 0
    problems: list[str] = field(default_factory=list)
    coverage: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return f"schema ok ({self.products} products)"
        return "schema drift: " + "; ".join(self.problems)


def _present(product: dict[str, Any], path: str) -> bool:
    cur: Any = product
    for part in path.split("."):
        if not isinstance(cur, dict) or cur.get(part) in (None, "", [], {}):
            return False
        cur = cur[part]
    return True


def check_search_response(raw: Any) -> SchemaReport:
    """Validate one searchModel response and report drift. Never raises."""
    if not isinstance(raw, dict):
        return SchemaReport(False, problems=["response is not a JSON object"])
    try:
        parsed = SearchResponse.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return SchemaReport(False, problems=[f"unexpected structure at {where}: {first['msg']}"])
    if parsed.data is None or parsed.data.searchModel is None:
        return SchemaReport(False, problems=["data.searchModel is missing"])
    raw_products = [p for p in (raw["data"]["searchModel"].get("products") or []) if isinstance(p, dict)]
    report = SchemaReport(True, products=len(raw_products))
    if len(raw_products) < MIN_PRODUCTS_TO_JUDGE:
        # Too few products to tell drift from a sparse category.
        return report
    for path in (*_REQUIRED_SHARE, *_TRACKED_ONLY):
        share = sum(_present(p, path) for p in raw_products) / len(raw_products)
        report.coverage[path] = round(share, 2)
    for path, minimum in _REQUIRED_SHARE.items():
        if report.coverage[path] < minimum:
            report.problems.append(
                f"{path} is present on {report.coverage[path]:.0%} of products (expected {minimum:.0%}+)"
            )
    report.ok = not report.problems
    return report
