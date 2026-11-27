"""Turning one API page into what a walk records.

Pure functions over parsed data — no HTTP, no database — so the rules for what
a page contributes can be tested directly: which items count toward coverage,
which products belong to the configured brands, and which snapshots are new.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from hd.hd_api.parsers import parse_snapshots


def note_coverage(parsed: list[Any], coverage_ids: set[str]) -> bool:
    """Record every itemId this page returned (before any brand filter).

    Returns True when at least one id was new to the walk, which is what the
    walk's "nothing new for N pages" stop rules count.
    """
    new_ids = False
    for product in parsed:
        if product.item_id and product.item_id not in coverage_ids:
            coverage_ids.add(product.item_id)
            new_ids = True
    return new_ids


def select_products(
    parsed: list[Any], all_brands: bool, upper_brands: Collection[str]
) -> list[Any]:
    """Products worth keeping: branded, and in the configured set unless the
    walk captures every brand."""
    return [
        p for p in parsed
        if p.brand and (all_brands or p.brand.upper() in upper_brands)
    ]


def select_snapshots(
    raw: dict[str, Any],
    store_id: str,
    wanted_ids: Collection[str],
    seen_item_ids: Collection[str],
) -> list[Any]:
    """This store's snapshots for the wanted items, excluding any already
    recorded this run (an item can appear on several pages of a walk)."""
    return [
        s for s in parse_snapshots(raw, store_id)
        if s.item_id in wanted_ids and s.item_id not in seen_item_ids
    ]
