"""`hd init`: from a ZIP code to a working install, no questions.

The nearest stores to the ZIP are looked up through Home Depot's own store
finder (one request), the chosen ones are written with their city, state and
ZIP so the deals page can link them, and the rest is the same safe, re-runnable
path `hd quick-setup` uses. Nothing here is specific to any one town.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from hd.hd_api.stores import StoreResult
from hd.quick_setup import QuickSetupResult, run_quick_setup

DEFAULT_BRANDS = ("Milwaukee", "DEWALT", "Makita", "Ryobi")
DEFAULT_RADIUS_MILES = 25.0
DEFAULT_MAX_STORES = 2

Searcher = Callable[..., Awaitable[list[StoreResult]]]


@dataclass
class InitResult:
    zip_code: str
    radius_miles: float
    found: list[StoreResult] = field(default_factory=list)
    chosen: list[StoreResult] = field(default_factory=list)
    setup: QuickSetupResult | None = None  # None on --dry-run


def store_spec(store: StoreResult) -> str:
    """The `id|Name|City|ST|ZIP` form quick-setup takes."""
    parts = [store.store_id, store.name, store.city, store.state, store.zip]
    return "|".join((p or "").replace("|", " ").strip() for p in parts).rstrip("|")


def choose_stores(found: list[StoreResult], max_stores: int) -> list[StoreResult]:
    """The nearest stores, preferring ones with complete address details.

    A store missing city, state or ZIP scans fine but loses its store-page
    link, so complete ones win ties on distance; incomplete ones only fill
    remaining slots.
    """
    if max_stores < 1:
        raise ValueError("max_stores must be at least 1")

    def by_distance(s: StoreResult) -> float:
        return s.distance_miles if s.distance_miles is not None else float("inf")

    complete = sorted((s for s in found if s.is_complete), key=by_distance)
    partial = sorted((s for s in found if not s.is_complete), key=by_distance)
    return (complete + partial)[:max_stores]


async def run_init(
    root: Path,
    zip_code: str,
    *,
    radius_miles: float = DEFAULT_RADIUS_MILES,
    max_stores: int = DEFAULT_MAX_STORES,
    brands: list[str] | None = None,
    install_schedule: bool = True,
    dry_run: bool = False,
    searcher: Searcher | None = None,
) -> InitResult:
    """Find stores near `zip_code` and configure them. Raises the store-lookup
    errors from `hd.hd_api.stores` unchanged so the caller can explain them."""
    if searcher is None:
        from hd.hd_api.stores import search_stores

        searcher = search_stores

    result = InitResult(zip_code=zip_code.strip(), radius_miles=radius_miles)
    result.found = await searcher(result.zip_code, radius_miles=radius_miles, limit=15)
    result.chosen = choose_stores(result.found, max_stores)
    if not result.chosen or dry_run:
        return result

    result.setup = await run_quick_setup(
        root,
        [store_spec(s) for s in result.chosen],
        list(brands or DEFAULT_BRANDS),
        install_schedule=install_schedule,
    )
    return result
