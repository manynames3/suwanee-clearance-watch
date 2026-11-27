"""The retailer-adapter protocol and its registry."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from hd.hd_api.models import NormalizedProduct, NormalizedSnapshot


@runtime_checkable
class RetailerAdapter(Protocol):
    """What the pipeline needs from a retailer.

    Methods return raw upstream payloads or the shared normalized models, never
    retailer-specific types. `fulfillment` strings are the shared vocabulary:
    "active", "limited", "unavailable", "none" or None when unknown.
    """

    name: str

    async def discover_stores(
        self, zip_code: str, *, radius_miles: float = 25.0, limit: int = 15
    ) -> list[Any]:
        """Stores near a ZIP, nearest first. Empty when none are in range."""
        ...

    async def browse_category(
        self,
        client: Any,
        *,
        store_id: str,
        nav_param: str | None = None,
        keyword: str | None = None,
        start_index: int = 0,
        page_size: int = 24,
    ) -> dict[str, Any]:
        """One page of a category or keyword listing, as the raw response."""
        ...

    async def fetch_item_detail(
        self, client: Any, *, store_id: str, item_id: str
    ) -> dict[str, Any]:
        """An authoritative single-item lookup at one store."""
        ...

    def parse_fulfillment(self, item: dict[str, Any], store_id: str) -> str | None:
        """The in-store pickup state of one raw item at one store."""
        ...

    def parse_products(self, raw: dict[str, Any]) -> list[NormalizedProduct]: ...

    def parse_snapshots(self, raw: dict[str, Any], store_id: str) -> list[NormalizedSnapshot]: ...


_REGISTRY: dict[str, RetailerAdapter] = {}


def register_adapter(adapter: RetailerAdapter) -> None:
    if not isinstance(adapter, RetailerAdapter):
        raise TypeError(f"{adapter!r} does not satisfy RetailerAdapter")
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: str = "homedepot") -> RetailerAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown retailer {name!r}; known: {sorted(_REGISTRY)}") from None
