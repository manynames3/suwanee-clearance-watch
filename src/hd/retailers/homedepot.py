"""Home Depot, as a RetailerAdapter. A thin wrapper over `hd.hd_api`."""

from __future__ import annotations

from typing import Any

from hd.hd_api import graphql, parsers
from hd.hd_api.models import NormalizedProduct, NormalizedSnapshot
from hd.hd_api.stores import search_stores
from hd.retailers.base import register_adapter


class HomeDepotAdapter:
    name = "homedepot"

    async def discover_stores(
        self, zip_code: str, *, radius_miles: float = 25.0, limit: int = 15
    ) -> list[Any]:
        return await search_stores(zip_code, radius_miles=radius_miles, limit=limit)

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
        return await graphql.search(
            client,
            keyword=keyword,
            nav_param=nav_param,
            store_id=store_id,
            start_index=start_index,
            page_size=page_size,
        )

    async def fetch_item_detail(
        self, client: Any, *, store_id: str, item_id: str
    ) -> dict[str, Any]:
        # The verified single-item path: a keyword search on the item id.
        return await graphql.search(client, keyword=item_id, store_id=store_id, page_size=24)

    def parse_fulfillment(self, item: dict[str, Any], store_id: str) -> str | None:
        return parsers.bopis_state(item, store_id)

    def parse_products(self, raw: dict[str, Any]) -> list[NormalizedProduct]:
        return parsers.parse_products(raw)

    def parse_snapshots(self, raw: dict[str, Any], store_id: str) -> list[NormalizedSnapshot]:
        return parsers.parse_snapshots(raw, store_id)


register_adapter(HomeDepotAdapter())
