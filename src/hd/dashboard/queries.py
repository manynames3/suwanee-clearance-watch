"""Compatibility shim: the read-side queries now live in `hd.db.repository`.

The dashboard, the static site, the digest and the alert writer all read
through that one module, so nothing outside the UI imports from the UI.
"""

from __future__ import annotations

from hd.db.repository import (  # noqa: F401
    log,
    _latest_snapshots_subquery,
    _first_price_subquery,
    get_overview_stats,
    get_products_with_latest,
    get_product_detail,
    get_alerts,
    get_store_summary,
    get_deal_board,
    ONLINE_STORE_KEY,
    get_dismissals,
    _is_dismissed,
    dismiss_deal,
    restore_deal,
    ONLINE_DISPLAY_LIMIT,
    ONLINE_UNVERIFIED_SLOTS,
    ONLINE_WARNING_SLOTS,
    ONLINE_UNKNOWN_SLOTS,
    HOLLOW_CLAIM_MIN_DAYS,
    VERIFIED_MIN_PCT,
    deal_tier,
    store_page_url,
    _is_older_day,
    _as_utc,
    _is_recent,
    _promo_predicate,
    _online_rows_select,
    _fulfillment_verdicts,
    _deals_from_rows,
    get_online_deals,
    get_daily_deal_picks,
)
