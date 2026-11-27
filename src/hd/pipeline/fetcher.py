"""Fetching the walks: facet reads and page-by-page capture.

The network half of the browse. Each page is normalized by `normalizer` and
handed to the database through the existing snapshot/product writers.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from hd.config import Settings
from hd.hd_api.graphql import failure_reason, is_valid_search_response, search
from hd.hd_api.parsers import parse_dimensions, parse_products
from hd.http.client import CircuitOpenError, HDClient
from hd.pipeline.normalizer import note_coverage, select_products, select_snapshots
from hd.pipeline.nav_planner import Walk, _prime, _safe_label, both_ends_cap, plan_walks, reachable_cap

from hd.logging import get_logger

log = get_logger("pipeline.browse")


async def fetch_facets(
    client: HDClient,
    settings: Settings,
    nav_param: str,
    store_id: str,
    storefilter: str,
    page_size: int | None = None,
) -> tuple[int | None, dict[str, list[dict]], dict | None]:
    """One facet read: (totalProducts, dimensions, raw response).

    Fetches a full page rather than a single row by default. The counts and
    dimensions are the same either way, but a full page can then serve as page
    0 of the walk this read is planning — and most walks are one page, so
    asking for one row here meant paying for two requests to read one page of
    results. Callers that know the node will be split into differently-navved
    children pass page_size=1, since no page they fetch here is reusable.
    """
    raw = await search(
        client,
        keyword=None,
        nav_param=nav_param,
        store_id=store_id,
        start_index=0,
        page_size=page_size or settings.page_size,
        storefilter=storefilter,
    )
    if not is_valid_search_response(raw):
        log.error(
            "Facet read failed",
            nav_param=nav_param, store_id=store_id, storefilter=storefilter,
            reason=failure_reason(raw) or "api_error",
        )
        return None, {}, None
    search_model = raw.get("data", {}).get("searchModel") or {}
    total = (search_model.get("searchReport") or {}).get("totalProducts")
    return total, parse_dimensions(raw), raw


async def resolve_walks(
    client: HDClient,
    settings: Settings,
    nav_param: str,
    label: str,
    store_id: str,
    storefilter: str,
    total: int | None = None,
    dimensions: dict[str, list[dict]] | None = None,
    observed_totals: dict[str, int] | None = None,
    recent_axis: dict[str, str] | None = None,
    recorded_children: dict[str, list[tuple[str, str, int]]] | None = None,
    depth: int = 0,
) -> list[Walk]:
    """Plan walks for a node, fetching facets for any piece still over the cap.

    `depth` is how many splits already lie above `nav_param`. A caller that
    starts below the top of a category passes it, so the node is planned at
    the depth it has when reached through that category: the split-depth guard
    in plan_walks is tested before anything else, and a node planned one level
    too shallow is split where the other route would walk it whole.
    """
    raw = None
    if total is None or dimensions is None:
        total, dimensions, raw = await fetch_facets(
            client, settings, nav_param, store_id, storefilter
        )
    walks, need = plan_walks(nav_param, label, total, dimensions, settings,
                             depth=depth,
                             observed_totals=observed_totals,
                             recent_axis=recent_axis,
                             recorded_children=recorded_children)
    _prime(walks, nav_param, raw)
    depth += 1
    # Carry the node each pending child was split OUT OF alongside it. Only the
    # category branch produces `need`, so the axis is always "category"; the
    # parent is whichever plan_walks call handed the child back.
    #
    # It is needed because plan_walks cannot tag these itself. Called on a
    # `need` child it sees only that child's own navParam and has no idea the
    # node was handed to it as a piece of something bigger, so whenever it
    # decides the child is a walk in its own right — under the cap after all,
    # both-ends, or truncated — it returns an untagged walk and that piece of
    # the parent's split records no split. Reachable and not rare: `need` is
    # populated from the PARENT's claimed count, the child's own page 0 is then
    # read, and the two disagree often enough that this file already carries a
    # correction for it. A missing child is exactly the silent omission that
    # made the withdrawn roll-up unsound, so the caller supplies what the
    # callee cannot know.
    pending: list[tuple[str, str, str]] = [(n, lb, nav_param) for n, lb in need]
    while pending and depth <= settings.browse_max_split_depth:
        next_pending: list[tuple[str, str, str]] = []
        for child_nav, child_label, parent_nav in pending:
            if client.is_throttled:
                return walks
            c_total, c_dims, c_raw = await fetch_facets(
                client, settings, child_nav, store_id, storefilter
            )
            c_walks, c_need = plan_walks(
                child_nav, child_label, c_total, c_dims, settings,
                depth=depth, price_split_used=False,
                observed_totals=observed_totals,
                # The node that flips axis is a depth-1 child, not a root, so
                # the preference has to reach this call to do anything at all.
                recent_axis=recent_axis,
                recorded_children=recorded_children,
            )
            _prime(c_walks, child_nav, c_raw)
            for w in c_walks:
                # Only the walk that IS this child. Anything plan_walks split
                # further already carries the child as its own split_parent,
                # and overwriting that would name a grandparent.
                if w.split_parent is None and w.nav_param == child_nav:
                    w.split_parent = parent_nav
                    w.split_axis = "category"
            walks.extend(c_walks)
            next_pending.extend((n, lb, child_nav) for n, lb in c_need)
        pending = next_pending
        depth += 1
    # Belt and braces: with the depth guard inside plan_walks firing at exactly
    # browse_max_split_depth, the last iteration above can never hand anything
    # back, so this loop does not currently run — traced with a router that
    # yields a fresh over-cap child at every level, and the chain terminated on
    # plan_walks' own "truncated-depth" branch instead. Left in place, and
    # tagged like any other piece of a split, because it is the fallback for a
    # loop bound changing.
    for child_nav, child_label, parent_nav in pending:
        log.warning("Unsplit oversized node — walking reachable head only", label=child_label)
        walks.append(Walk(child_nav, child_label, reachable_cap(settings) + 1,
                          truncated=True,
                          split_parent=parent_nav, split_axis="category"))
    return walks


async def _page_direction(
    client: HDClient,
    settings: Settings,
    walk: Walk,
    store_id: str,
    storefilter: str,
    seen_item_ids: set[str],
    upper_brands: list[str],
    now: datetime,
    *,
    order_by: dict[str, str] | None,
    coverage_ids: set[str],
    expect_ceiling: bool = False,
    coverage_target: int | None = None,
) -> tuple[int, int, int | None]:
    """Page one direction of a walk from startIndex 0 to the API ceiling.

    Records every itemId the API returns (pre-brand-filter) in coverage_ids so a
    both-ends walk can verify its union spanned the node. order_by=None is the
    default BEST_MATCH ordering and the only one that may reuse a primed page — a
    primed facet read is BEST_MATCH and is the wrong page for a price ordering.
    expect_ceiling silences the "remainder not covered" warning for a both-ends
    direction, where reaching the ceiling is the intended half-coverage, not a
    truncation (the union assertion in the caller judges coverage instead).
    coverage_target stops this direction once the shared union has seen every
    item (plus a confirmation buffer), so the second pass need not run to the
    ceiling. Returns (upserts, inserts, total_from_page0).
    """
    # Imported here to keep module import light and avoid cycles.
    from hd.pipeline.discovery import _upsert_products
    from hd.pipeline.snapshot import _insert_snapshots, _write_raw_json

    products_upserted = 0
    snapshots_inserted = 0
    observed_total: int | None = None
    confirm_left: int | None = None   # counts down once coverage first hits target
    stale_pages = 0                   # consecutive pages that added no new itemId
    page = 0

    while True:
        start_index = page * settings.page_size
        if start_index > settings.api_max_start_index:
            if not walk.truncated and not expect_ceiling:
                log.warning(
                    "Hit API startIndex ceiling mid-walk — remainder not covered",
                    label=walk.label, total=walk.total,
                )
            break

        if page == 0 and order_by is None and walk.primed is not None:
            # Already fetched while planning this walk; consumed once so a
            # later pass cannot serve a stale page.
            raw = walk.primed
            walk.primed = None
        else:
            try:
                raw = await search(
                    client,
                    keyword=None,
                    nav_param=walk.nav_param,
                    store_id=store_id,
                    start_index=start_index,
                    page_size=settings.page_size,
                    storefilter=storefilter,
                    order_by=order_by,
                )
            except CircuitOpenError:
                # Too many failures too fast. This ends the run, not just this
                # walk, so it has to travel past the per-walk handler.
                raise
            except Exception as e:
                log.error("Browse page fetch failed", label=walk.label, page=page, error=str(e))
                walk.cut = True
                break

        if client.is_throttled:
            walk.cut = True
            break

        if not is_valid_search_response(raw):
            log.error(
                "Browse page unusable — coverage incomplete",
                label=walk.label, store_id=store_id, page=page,
                storefilter=storefilter,
                reason=failure_reason(raw) or "api_error",
            )
            walk.cut = True
            break

        search_model = raw.get("data", {}).get("searchModel") or {}
        raw_products = search_model.get("products") or []

        if page == 0:
            observed_total = (search_model.get("searchReport") or {}).get("totalProducts")
            log.info(
                "Walk started",
                label=walk.label, store_id=store_id, storefilter=storefilter,
                total_products=observed_total, planned=walk.total,
                order=(order_by or {}).get("order"),
            )

        if settings.store_raw_json:
            # Direction in the filename so a both-ends walk's two passes don't
            # collide: ASC and DESC restart page at 0 and share `now` (one
            # per-walk timestamp), so without this DESC would silently overwrite
            # ASC's early pages in raw_responses. Empty for a single walk, so
            # every existing filename and offline analysis keyed on them is
            # unchanged.
            direction = ""
            if order_by:
                direction = "_asc" if order_by.get("order") == "ASC" else "_desc"
            await _write_raw_json(
                settings,
                f"browse_{_safe_label(walk.label)}_{storefilter}{direction}_p{page}",
                store_id, now, raw,
            )

        parsed = parse_products(raw)
        new_ids_this_page = note_coverage(parsed, coverage_ids)
        stale_pages = 0 if new_ids_this_page else stale_pages + 1

        products = select_products(parsed, walk.all_brands, upper_brands)
        if products:
            products_upserted += await _upsert_products(settings, products)

        wanted_ids = {p.item_id for p in products if p.item_id}
        snapshots = select_snapshots(raw, store_id, wanted_ids, seen_item_ids)
        if snapshots:
            snapshots_inserted += await _insert_snapshots(
                settings, snapshots, store_id, now, walk.nav_param,
            )
            seen_item_ids.update(s.item_id for s in snapshots)

        # Early stop for the second (DESC) pass: once the union has seen every
        # item, confirm with a few pages that turn up nothing new, then stop
        # instead of running to the ceiling. Resetting the counter whenever a
        # page still yields new itemIds makes the rule "N pages with nothing
        # new", not "N pages" — so an UNDERSTATED totalProducts self-extends
        # rather than leaving a silent gap (the mirror of grew_past_cap, which
        # guards the overstated direction). Completeness then rests on "no new
        # items", never on the count being right.
        if coverage_target is not None and len(coverage_ids) >= coverage_target:
            if confirm_left is None or new_ids_this_page:
                confirm_left = settings.both_ends_confirm_pages
            if confirm_left <= 0:
                break
            confirm_left -= 1

        if not raw_products:
            break

        if len(raw_products) < settings.page_size:
            # A short page is the ordinary end-of-set signal, and for a node
            # whose count is honest it still is: coverage has reached the total
            # and we stop exactly where we used to.
            #
            # It is NOT sufficient on its own. Home Depot also returns a short
            # page in the MIDDLE of a set — measured on Power Tools/Saws, whose
            # page 2 came back with 23 of 24 while its own searchReport claimed
            # 299. Treating that as the end stopped the walk at 71 items and
            # left 220 of them unobserved for days, with no other walk covering
            # them. The same cut hit the shelf's ASC direction at page 28,
            # which is what opened the union gap that made the walk truncated.
            #
            # So the short page only ends the walk once coverage says the set
            # is done. Otherwise keep paging, bounded by the same rule the
            # both-ends confirm uses — N pages that turn up nothing new — so an
            # OVERSTATED total costs a few pages, never an unbounded walk.
            target = coverage_target or observed_total
            if target is None or len(coverage_ids) >= target:
                break
            if stale_pages > settings.short_page_confirm_pages:
                log.warning(
                    "Short page with coverage still incomplete — stopping, "
                    "nothing new for several pages",
                    label=walk.label, page=page, covered=len(coverage_ids),
                    total=target,
                )
                break
            log.info(
                "Short page mid-set — continuing, coverage incomplete",
                label=walk.label, page=page, got=len(raw_products),
                covered=len(coverage_ids), total=target,
            )
        page += 1

    return products_upserted, snapshots_inserted, observed_total


async def walk_and_capture(
    client: HDClient,
    settings: Settings,
    walk: Walk,
    store_id: str,
    storefilter: str,
    seen_item_ids: set[str],
    brands: list[str],
) -> tuple[int, int]:
    """Paginate one walk; upsert products and append snapshots per page.

    Returns (products_upserted, snapshots_inserted). Stops on throttle,
    invalid response, short page, or the API's startIndex ceiling. A both-ends
    walk pages the cheap end and the dear end and asserts their union covered the
    node; a shortfall marks the walk truncated rather than silently under-covering.
    """
    now = datetime.now(timezone.utc)
    upper_brands = [b.upper() for b in brands]
    coverage: set[str] = set()

    if not walk.both_ends:
        up, ins, live_total = await _page_direction(
            client, settings, walk, store_id, storefilter, seen_item_ids,
            upper_brands, now, order_by=None, coverage_ids=coverage,
        )
        walk.live_total = live_total
        walk.observed_ids = len(coverage)
        return up, ins

    # ASC runs full — it can only reach the cheap 744, never the whole node —
    # and hands back the AUTHORITATIVE total it read on page 0, which is the
    # denominator the assertion must use: the node may have grown since planning.
    up_a, ins_a, live_total = await _page_direction(
        client, settings, walk, store_id, storefilter, seen_item_ids,
        upper_brands, now, order_by={"field": "PRICE", "order": "ASC"},
        coverage_ids=coverage, expect_ceiling=True,
    )
    if client.is_throttled:
        # ASC-only is structurally half a node — say so, don't report it clean.
        walk.truncated = True
        walk.live_total = live_total
        walk.observed_ids = len(coverage)
        return up_a, ins_a

    up_d, ins_d, _ = await _page_direction(
        client, settings, walk, store_id, storefilter, seen_item_ids,
        upper_brands, now, order_by={"field": "PRICE", "order": "DESC"},
        coverage_ids=coverage, expect_ceiling=True,
        coverage_target=live_total,
    )

    # Union assertion — the hard gate, against the LIVE total (planned only as a
    # fallback). A node that grew past the both-ends cap since planning can't be
    # covered by two ends either, so that counts as short too. Surface via
    # walk.truncated, exactly like a facet-split truncation, so a seam gap can
    # never pass silently as full coverage.
    denom = live_total if live_total else walk.total
    covered = len(coverage)
    grew_past_cap = bool(live_total) and live_total > both_ends_cap(settings)
    if grew_past_cap or (denom and covered < denom):
        walk.truncated = True
        log.warning(
            "Both-ends coverage short — seam gap or node grew past cap",
            label=walk.label, covered=covered, total=denom,
            gap=(denom - covered) if denom else None, grew_past_cap=grew_past_cap,
        )
    else:
        log.info(
            "Both-ends coverage complete",
            label=walk.label, covered=covered, total=denom,
        )
    walk.live_total = live_total
    walk.observed_ids = covered
    return up_a + up_d, ins_a + ins_d


async def _pause(settings: Settings) -> None:
    await asyncio.sleep(random.uniform(
        settings.keyword_pause_min_seconds,
        settings.keyword_pause_max_seconds,
    ))
