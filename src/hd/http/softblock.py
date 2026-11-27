"""Silent soft-block detection.

A refusal does not always look like one. A 200 with a well-formed but empty (or
suspiciously short) product list reads as "nothing here" — and a scanner that
believes it records an empty shelf, then raises "gone from stock" alerts for
everything. The only defence is memory: remember how many products a category
normally holds, and treat a drastic shortfall on a category that used to be
populated as a refusal rather than as news.

The remembered totals live in a small JSON file beside the other run state.
A missing or corrupt file means nothing is "known populated" yet, so detection
simply stays quiet until totals have been learned — it fails open, never
inventing a block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hd.logging import get_logger

log = get_logger("http.softblock")

# A category must have held at least this many products before an empty answer
# counts as suspicious. Small categories legitimately empty out.
KNOWN_POPULATED_MIN = 48

# On a later page, fewer products than this fraction of what that page should
# hold (given the remembered total) is a truncation, not an end of results.
SHORTFALL_FRACTION = 0.5

# Bound on how many categories are remembered, so the file cannot grow forever.
MAX_REMEMBERED = 5000


def category_key(variables: dict[str, Any]) -> str:
    """Stable identity of a browse: keyword, node, store and storefilter."""
    return "|".join(
        str(variables.get(k) or "")
        for k in ("keyword", "navParam", "storeId", "storefilter")
    )


def _products(data: dict[str, Any]) -> list | None:
    try:
        products = data["data"]["searchModel"]["products"]
    except (KeyError, TypeError):
        return None
    return products if isinstance(products, list) else None


def _reported_total(data: dict[str, Any]) -> int | None:
    try:
        total = data["data"]["searchModel"]["searchReport"]["totalProducts"]
    except (KeyError, TypeError):
        return None
    return total if isinstance(total, int) else None


class SoftBlockDetector:
    """Learns category sizes from good pages; flags implausible ones."""

    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None
        self._known: dict[str, int] = self._load()
        self._dirty = False

    def _load(self) -> dict[str, int]:
        if self._path is None:
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}

    def known_total(self, variables: dict[str, Any]) -> int | None:
        return self._known.get(category_key(variables))

    def assess(self, variables: dict[str, Any], data: dict[str, Any]) -> str | None:
        """A reason string if this answer looks like a soft block, else None.

        Also learns: a plausible page updates the remembered total.
        """
        products = _products(data)
        if products is None:
            return None  # not a search payload; other checks own that case

        key = category_key(variables)
        known = self._known.get(key)
        start = int(variables.get("startIndex") or 0)
        size = int(variables.get("pageSize") or 0)
        reported = _reported_total(data)

        if known is not None and known >= KNOWN_POPULATED_MIN:
            if start == 0 and not products:
                return (
                    f"first page empty for a category that held {known} products"
                )
            if start == 0 and reported == 0:
                return f"total reported as 0 for a category that held {known}"
            expected_here = min(size, max(0, known - start))
            # Only judge pages comfortably inside the known range, so ordinary
            # shrinkage near the end of a category is not mistaken for a block.
            if size and start + size <= int(known * 0.8):
                if len(products) < expected_here * SHORTFALL_FRACTION:
                    return (
                        f"page at {start} returned {len(products)} of ~{expected_here} "
                        f"expected for a category that held {known}"
                    )

        if start == 0 and products and reported and reported > 0:
            self._remember(key, reported)
        return None

    def _remember(self, key: str, total: int) -> None:
        if self._known.get(key) == total:
            return
        if key not in self._known and len(self._known) >= MAX_REMEMBERED:
            return
        self._known[key] = total
        self._dirty = True

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._known, sort_keys=True))
            tmp.replace(self._path)
            self._dirty = False
        except OSError as exc:
            log.warning("Could not persist known category totals", error=str(exc))
