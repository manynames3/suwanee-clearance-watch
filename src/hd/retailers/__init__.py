"""Retailer adapters: the seam between the pipeline and one store chain.

The pipeline (diffing, confidence, alerts, notifiers, site) never talks to a
retailer directly. It talks to a `RetailerAdapter`, and Home Depot is the first
implementation. A second retailer is a new adapter module plus an entry in
`ADAPTERS`; nothing downstream changes.
"""

from __future__ import annotations

from hd.retailers.base import RetailerAdapter, get_adapter, register_adapter
from hd.retailers.homedepot import HomeDepotAdapter

__all__ = ["RetailerAdapter", "HomeDepotAdapter", "get_adapter", "register_adapter"]
