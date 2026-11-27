"""Facet-driven brand browse: discovery + snapshots in a single pass.

Why this exists: keyword search scans structurally miss clearance deals.
HD's keyword relevance excludes brand items outside the scanned category
(missed deals lived in Plumbing and Garage while every scan was Tools-scoped),
drops some brand items from keyword result sets entirely, and the API refuses
startIndex > a fixed ceiling, so large result sets can never be fully paged.

Browse mode walks the brand's own category facets instead — the same data the
website's left nav renders. The response's `dimensions` block supplies every
category token with a per-store item count, so coverage is complete,
deterministic, and self-maintaining: category drift shows up in the next facet
read, not as a silent coverage hole.

Two tiers:

- **shelf** (storefilter=IN_STORE): equivalent to the "Pick Up Today" BOPIS
  facet — items physically assorted to the store (~1.4k/store). Small enough
  to sweep fully every run; catches on-shelf clearance.
- **network** (storefilter=ALL): the full brand set (~9k). Catches
  ship-to-store (BOSS) clearance that IN_STORE hides. Too big for one polite
  run, so categories rotate across runs via a cursor file; each category is
  covered completely when its turn comes.

Both tiers upsert products AND append snapshots from the same pages, so a
newly discovered item is monitored the same run it is first seen.

A network run can also be given a LEAD: nodes it walks before the rotation at
configured hours, for prices that change on the retailer's daily clock rather
than whenever (see `browse_network_lead` in config and `run_network_lead`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from hd import rotation
from hd.config import Settings
from hd.http.client import CircuitOpenError, HDClient
from hd.logging import get_logger
from hd.pipeline.nav_planner import (  # noqa: F401  re-exported: callers import these from here
    BrowseSummary,
    CATEGORY_DIMENSION,
    KNOWN_CONTAINED_PRICE_LABELS,
    PRICE_BAND_LABEL,
    PRICE_DIMENSION,
    SCHEDULE_TZ,
    Walk,
    _count,
    _prime,
    _safe_label,
    admission_ceiling,
    admits,
    advance_brand_order,
    both_ends_cap,
    brand_order_cursor_key,
    brand_order_for_run,
    budget_spent,
    build_nav,
    current_et_hour,
    effective_shelf_fraction,
    full_shelf_hours,
    network_lead_for_run,
    parse_et_hours,
    plan_walks,
    price_partition,
    reachable_cap,
    rotate_shelf_walks,
    walk_cost_estimate,
    walk_status,
)
from hd.pipeline.fetcher import (  # noqa: F401  re-exported: callers import these from here
    _page_direction,
    _pause,
    fetch_facets,
    resolve_walks,
    walk_and_capture,
)
from hd.pipeline.persister import (  # noqa: F401  re-exported: callers import these from here
    _record_run_end,
    _record_run_start,
    _record_walk,
    coverage_memory,
)

log = get_logger("pipeline.browse")


@dataclass
class LeadOutcome:
    """What the lead did, in the terms the rotation needs to hear it."""

    # Every navParam the lead ATTEMPTED, complete or not. The rotation treats
    # these as done for this run: a walk the lead truncated minutes ago would
    # otherwise pass the freshness filter (which knows only completions) and
    # be paged a second time for nothing.
    walked: set[str] = field(default_factory=set)
    # NavParams the lead refused for lack of budget. The rotation re-plans
    # their category later in the run and, planning from the same counts with
    # less budget left, refuses them again. That is the same refusal reached a
    # second time, and it must not be counted a second time.
    deferred: set[str] = field(default_factory=set)


async def locate_lead_node(
    client: HDClient,
    settings: Settings,
    store_id: str,
    brand: str,
    token: str,
    path: tuple[str, ...],
    facets: dict[str, tuple[int | None, dict[str, list[dict]]]],
) -> tuple[str, str, str] | None:
    """Find a lead node by its label path: (navParam, label, parent navParam).

    One facet read per level, shared across entries through `facets`. The
    label is assembled from the facets' own labels exactly as the rotation
    assembles it, never from the configured text: two spellings for one
    navParam would split everything downstream that groups walks by label.

    None when the path cannot be followed. The caller carries on with the
    ordinary rotation — a lead that cannot be found must not cost the run.
    """
    nav = build_nav(settings.root_nav_param, token)
    label = brand
    parent = nav
    for segment in path:
        if client.is_throttled or budget_spent(client, settings):
            return None
        if nav not in facets:
            total, dims, _ = await fetch_facets(
                client, settings, nav, store_id, "ALL", page_size=1
            )
            facets[nav] = (total, dims)
        total, dims = facets[nav]
        in_nav = set(nav.split("Z"))
        offered = [
            r for r in dims.get(CATEGORY_DIMENSION, [])
            if r.get("count") and r["count"] > 0 and r["token"] not in in_nav
        ]
        wanted = segment.casefold()
        matches = [
            r for r in offered
            if (r.get("label") or "").casefold() == wanted or r["token"] == segment
        ]
        if len(matches) != 1:
            log.warning(
                "Lead node not found — continuing with the ordinary rotation",
                brand=brand, path="/".join(path), segment=segment,
                reason="facet read failed" if total is None else
                       "no such category" if not matches else "ambiguous",
                offered=[r.get("label") or r["token"] for r in offered],
            )
            return None
        parent = nav
        nav = build_nav(nav, matches[0]["token"])
        label = f"{label}/{matches[0].get('label') or matches[0]['token']}"
    return nav, label, parent


def _remember_lead_split(
    walks: list[Walk],
    lead_nav: str,
    recent_axis: dict[str, str],
    recorded_children: dict[str, list[tuple[str, str, int]]],
) -> None:
    """Tell this run's memory how the lead just split its node.

    The rotation re-resolves the lead's category later in the same run, with a
    NEW facet read. If that read offers a different axis than the lead's did,
    the children carry navParams nobody has marked done and the whole node is
    walked again — the flip described above plan_walks, inside one run. The
    coverage rows that would prevent it are written, but `coverage_memory` was
    read before the lead ran. So hand the planner what the next run's memory
    would have said.

    Only for parents at or under the lead node. A lead node that turned out to
    be a single walk is tagged with ITS parent, and recording that as the
    parent's whole split would let a rebuild-from-record drop its siblings.
    """
    prefix = lead_nav + "Z"
    by_parent: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    for walk in walks:
        parent, axis = walk.split_parent, walk.split_axis
        if not parent or not axis:
            continue
        if parent != lead_nav and not parent.startswith(prefix):
            continue
        by_parent.setdefault((parent, axis), []).append(
            (walk.nav_param, walk.label, int(walk.total or 0))
        )
    for (parent, axis), kids in by_parent.items():
        recent_axis[parent] = axis
        recorded_children[parent] = sorted(kids)


async def _capture_walk(
    client: HDClient,
    settings: Settings,
    summary: BrowseSummary,
    run_id: int | None,
    walk: Walk,
    store_id: str,
    storefilter: str,
    seen: set[str],
    brands: list[str],
) -> None:
    """Walk one node and account for it the way the tier loops do.

    The tier loops carry this block inline. They are left as they are so that
    adding the lead changes nothing on the paths that already run.
    """
    started = datetime.now(timezone.utc)
    upserts, inserts = await walk_and_capture(
        client, settings, walk, store_id, storefilter, seen, brands,
    )
    await _record_walk(settings, run_id, walk, store_id, storefilter, started)
    summary.products += upserts
    summary.snapshots += inserts
    summary.walks += 1
    if walk.truncated:
        summary.truncated_walks.append(walk.label)
    await _pause(settings)


async def _plan_lead(
    client: HDClient,
    settings: Settings,
    store_id: str,
    lead: tuple[str, str, tuple[str, ...]],
    facets: dict[str, tuple[int | None, dict[str, list[dict]]]],
    memory: tuple[dict[str, int], dict[str, str], dict[str, list[tuple[str, str, int]]]],
) -> list[Walk]:
    """The walks for one lead entry, planned as the rotation would plan them.

    Empty when the node cannot be found, read or afforded. Everything the
    rotation needs in order to recognise these walks when it re-plans the same
    category later in the run is put into `memory` here.
    """
    brand, token, path = lead
    observed_totals, recent_axis, recorded_children = memory
    node = await locate_lead_node(client, settings, store_id, brand, token, path, facets)
    if node is None or budget_spent(client, settings):
        return []
    nav, label, parent = node
    total, dims, raw = await fetch_facets(client, settings, nav, store_id, "ALL")
    if not total:
        return []
    # The node's own count, where the rotation will look for it. Its category
    # states a count for it too, and that claim is sometimes several times too
    # small (see plan_walks). Planned from the claim, the rotation would route
    # this node differently than the lead just did — walk whole what the lead
    # split, or admit what the lead could not afford.
    observed_totals[nav] = max(observed_totals.get(nav, 0), int(total))
    walks = await resolve_walks(
        client, settings, nav, label, store_id, "ALL",
        total=total, dimensions=dims, depth=len(path) - 1,
        observed_totals=observed_totals,
        recent_axis=recent_axis,
        recorded_children=recorded_children,
    )
    _prime(walks, nav, raw)
    for walk in walks:
        # Reached through its category, a node that is a walk in its own right
        # is tagged as a piece of that category's split (see resolve_walks).
        # Reached directly it has no idea it has a parent. A top-level
        # category has none, and the rotation leaves it untagged too.
        if len(path) > 1 and walk.split_parent is None and walk.nav_param == nav:
            walk.split_parent, walk.split_axis = parent, "category"
    _remember_lead_split(walks, nav, recent_axis, recorded_children)
    log.info("Network lead", store_id=store_id, label=label, walks=len(walks))
    return walks


async def run_network_lead(
    client: HDClient,
    settings: Settings,
    summary: BrowseSummary,
    run_id: int | None,
    store_id: str,
    leads: list[tuple[str, str, tuple[str, ...]]],
    memory: tuple[dict[str, int], dict[str, str], dict[str, list[tuple[str, str, int]]]],
    seen: set[str],
    brands: list[str],
) -> LeadOutcome:
    """Walk the configured lead nodes before the rotation gets the run.

    Freshness is deliberately ignored: the point of a lead is a read at THIS
    hour, and a node the rotation happened to walk four hours ago is exactly
    the case it exists for. Everything else is the rotation's own machinery —
    the same planner at the same depth, the same admission test, the same
    coverage rows — so a lead walk writes the row the rotation would have
    written for that node, and resume and axis memory keep working on it.

    It writes no cursor. What it completes can leave the rotation finding a
    category already fresh, and the rotation counts that as it always has: the
    category is advanced and the brand has had a turn. The brand-lead baseline
    is taken AFTER this returns, so the lead's own walks are never what hands
    the lead on.
    """
    outcome = LeadOutcome()
    facets: dict[str, tuple[int | None, dict[str, list[dict]]]] = {}
    for lead in leads:
        if client.is_throttled or budget_spent(client, settings):
            # Not an error, but not silent either: a lead that did not run
            # should say so in the run's own log.
            log.info(
                "Network lead skipped — the run has no budget left for it",
                store_id=store_id, brand=lead[0], path="/".join(lead[2]),
                used=client.request_count, throttled=client.is_throttled,
            )
            break
        planned = await _plan_lead(client, settings, store_id, lead, facets, memory)
        # Two entries can name one node (a label and its token); walk it once.
        walks = [w for w in planned if w.nav_param not in outcome.walked]
        for position, walk in enumerate(walks):
            if client.is_throttled:
                break
            if not admits(client, settings, walk):
                log.info(
                    "Lead walk deferred — run cannot afford to finish it",
                    label=walk.label,
                    estimate=walk_cost_estimate(walk, settings),
                    used=client.request_count,
                    ceiling=admission_ceiling(settings),
                )
                # Every walk the break abandons, not just this one.
                refused = {w.nav_param for w in walks[position:]} - outcome.deferred
                summary.deferred_walks += len(refused)
                outcome.deferred |= refused
                break
            await _capture_walk(
                client, settings, summary, run_id, walk, store_id, "ALL", seen, brands,
            )
            outcome.walked.add(walk.nav_param)
    return outcome


async def run_browse(
    settings: Settings,
    store_ids: list[str] | None = None,
    client: HDClient | None = None,
    tiers: tuple[str, ...] = ("shelf", "network"),
    now: datetime | None = None,
) -> BrowseSummary:
    """Run the facet-driven browse scan for each store and configured brand.

    `now` is the instant the run is deemed to have started. The caller that
    chose the tiers from the clock passes the same instant here, so the tier
    choice and the lead decision cannot disagree across an hour boundary.
    """
    store_ids = store_ids or settings.store_list
    brand_tokens = settings.brand_token_list
    brands = [b for b, _ in brand_tokens]
    owns_client = client is None
    client = client or HDClient(settings, request_budget=settings.browse_request_budget)
    summary = BrowseSummary()
    seen_by_store: dict[str, set[str]] = {s: set() for s in store_ids}

    cursors: dict[str, int] = rotation.load_cursors(settings.browse_cursor_path)
    shelf_fraction = effective_shelf_fraction(settings, now)
    network_lead = network_lead_for_run(settings, tiers, now)

    if not brand_tokens:
        log.error("No brand tokens configured — browse cannot run")
        return summary

    log.info(
        "Browse starting",
        shelf_fraction=shelf_fraction,
        full_walk=shelf_fraction >= 1.0,
        stores=len(store_ids),
    )

    run_id = await _record_run_start(settings, tiers)

    try:
        if "shelf" in tiers:
            for store_id in store_ids:
                # Rotate which brand goes FIRST, one step per run.
                #
                # The per-brand cursor keeps a brand fair to itself across
                # runs, but nothing kept the brands fair to each other: this
                # loop walked the CSV in fixed order and breaks on budget, so
                # a run that ran short always starved whichever brand is last.
                # Because the per-brand cursor resumes rather than restarts,
                # that brand is not starved to zero — it is SLOWED, completing
                # its cycle over more days than the first. For a record whose
                # whole claim is witnessed price history that is not cosmetic:
                # price-days are the currency of the maturity ladder, so a
                # permanently-second brand reaches each level later by
                # construction, and cross-brand comparisons then rest on
                # uneven observation cadence.
                #
                # Invisible with one brand configured, guaranteed with two.
                #
                # A brand in `network_only_brands` is not shelf-walked at all:
                # its in-store size is unmeasured, and the shelf tier is the
                # one whose cost a brand's size can push past the ceiling.
                shelf_tokens = [
                    (b, t) for b, t in brand_tokens
                    if b.upper() not in settings.network_only_brand_list
                ]
                if brand_tokens and not shelf_tokens:
                    # Every configured brand is network-only: the tier walks
                    # nothing, and "Shelf tier complete" below would read as
                    # a pass. Say so, once, in the run's own log.
                    log.warning(
                        "Shelf tier has no brand to walk — every brand is network-only",
                        store_id=store_id,
                        network_only=settings.network_only_brand_list,
                    )
                order = brand_order_for_run(shelf_tokens, cursors, store_id, tier="shelf")
                # Same advance-on-progress rule as the network tier.
                shelf_lead_key = f"shelf|{store_id}|{order[0][1]}" if order else ""
                shelf_lead_cursor_before = cursors.get(shelf_lead_key)
                shelf_walks_before_lead = summary.walks
                for brand, token in order:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    if budget_spent(client, settings):
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    resolved = await resolve_walks(
                        client, settings, nav, brand, store_id, "IN_STORE",
                    )
                    # rotate_shelf_walks advances its cursor over what it
                    # SELECTS. The network tier deliberately advances only over
                    # what it FINISHES, for the reason spelled out there: a run
                    # cut short otherwise skips its unwalked tail until the
                    # cursor wraps. Capture the pre-advance position so the same
                    # guarantee can be restored here if this run stops early.
                    #
                    # Only when rotation actually rotates, though. It returns
                    # early — unsorted and without touching the cursor — on the
                    # same condition tested here, and the cursor indexes the
                    # LABEL-SORTED order. Rewinding against the unsorted list
                    # would write an index meaning a different category than the
                    # one we stopped on, skipping a node the next run should
                    # have walked. That path is live whenever a full-shelf hour
                    # fires (the shipped BROWSE_FULL_SHELF_HOURS_ET default).
                    rotates = shelf_fraction < 1.0 and len(resolved) > 1
                    shelf_key = f"shelf|{store_id}|{token}"
                    shelf_start = (
                        cursors.get(shelf_key, 0) % len(resolved) if resolved else 0
                    )
                    walks, skipped = rotate_shelf_walks(
                        resolved, cursors, store_id, token, settings, shelf_fraction
                    )
                    summary.skipped_walks += skipped
                    # Position of the first walk this run did not ATTEMPT —
                    # deferred on budget, or cut off by a throttle before it
                    # started. None means every selected walk was attempted.
                    # Attempted-but-truncated deliberately does NOT count; see
                    # the note at the truncation branch below.
                    first_incomplete: int | None = None
                    for position, walk in enumerate(walks):
                        if client.is_throttled:
                            summary.aborted = True
                            if first_incomplete is None:
                                first_incomplete = position
                            break
                        if not admits(client, settings, walk):
                            log.info(
                                "Walk deferred — run cannot afford to finish it",
                                label=walk.label, tier="IN_STORE",
                                estimate=walk_cost_estimate(walk, settings),
                                used=client.request_count,
                                ceiling=admission_ceiling(settings),
                            )
                            # Every walk from here on is abandoned, not just
                            # this one — count them all, or the summary
                            # understates what the run did not do.
                            summary.deferred_walks += len(walks) - position
                            if first_incomplete is None:
                                first_incomplete = position
                            break
                        walk_started = datetime.now(timezone.utc)
                        upserts, inserts = await walk_and_capture(
                            client, settings, walk, store_id, "IN_STORE",
                            seen_by_store[store_id], brands,
                        )
                        await _record_walk(
                            settings, run_id, walk, store_id, "IN_STORE", walk_started
                        )
                        summary.products += upserts
                        summary.snapshots += inserts
                        summary.walks += 1
                        if walk.truncated:
                            summary.truncated_walks.append(walk.label)
                            # Deliberately NOT treated as unfinished for the
                            # rewind. A truncated walk ran, captured what it
                            # could, and wrote its (truncated) coverage row —
                            # unlike a deferred walk, which did nothing. Some
                            # nodes truncate every single time they are walked:
                            # a node permanently over the cap with no facet to
                            # split it, or the shelf's both-ends walk, which
                            # goes short on roughly half its runs. Rewinding
                            # onto one pins the cursor there and starves every
                            # category behind it. It comes round again on its
                            # own turn instead.
                        await _pause(settings)
                    if rotates and first_incomplete is not None:
                        # Rewind onto the first walk this run did not finish,
                        # so the next one resumes there instead of rotating
                        # past it.
                        cursors[shelf_key] = (
                            (shelf_start + first_incomplete) % len(resolved)
                        )
                # Store-wide category nodes (e.g. the grill wall). Deliberately
                # outside shelf rotation — their value is every-pass coverage
                # and they are a few pages each — and captured for ALL brands:
                # the node defines the scope, so the brand filter would silently
                # drop most of what the walk was configured to see.
                for label, token in settings.shelf_category_walk_list:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    if budget_spent(client, settings):
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    walks = await resolve_walks(
                        client, settings, nav, label, store_id, "IN_STORE",
                    )
                    for position, walk in enumerate(walks):
                        walk.all_brands = True
                        if client.is_throttled:
                            summary.aborted = True
                            break
                        if not admits(client, settings, walk):
                            log.info(
                                "Walk deferred — run cannot afford to finish it",
                                label=walk.label, tier="IN_STORE",
                                estimate=walk_cost_estimate(walk, settings),
                                used=client.request_count,
                                ceiling=admission_ceiling(settings),
                            )
                            # The break abandons every remaining walk, not
                            # just this one. Counting 1 here understated the
                            # deferral — and this counter is the admission
                            # experiment's instrument, so the understatement
                            # lands directly on a gate decision.
                            summary.deferred_walks += len(walks) - position
                            break
                        walk_started = datetime.now(timezone.utc)
                        upserts, inserts = await walk_and_capture(
                            client, settings, walk, store_id, "IN_STORE",
                            seen_by_store[store_id], brands,
                        )
                        await _record_walk(
                            settings, run_id, walk, store_id, "IN_STORE", walk_started
                        )
                        summary.products += upserts
                        summary.snapshots += inserts
                        summary.walks += 1
                        if walk.truncated:
                            summary.truncated_walks.append(walk.label)
                        await _pause(settings)
                if order and (
                    summary.walks > shelf_walks_before_lead
                    or cursors.get(shelf_lead_key) != shelf_lead_cursor_before
                ):
                    advance_brand_order(
                        cursors, store_id, len(shelf_tokens), tier="shelf"
                    )
                log.info(
                    "Shelf tier complete",
                    store_id=store_id,
                    snapshots=summary.snapshots,
                    requests_used=client.request_count,
                    rotation_skipped=summary.skipped_walks or None,
                )

        if "network" in tiers and not client.is_throttled:
            per_run = max(1, settings.browse_network_categories_per_run)
            for store_id in store_ids:
                observed_totals, recently_done, recent_axis, recorded_children = await coverage_memory(
                    settings, store_id, "ALL", settings.browse_walk_refresh_hours,
                )
                # The lead goes before everything the rotation decides, and
                # before the brand-lead baseline below is taken.
                lead_deferred: set[str] = set()
                if network_lead:
                    lead = await run_network_lead(
                        client, settings, summary, run_id, store_id, network_lead,
                        (observed_totals, recent_axis, recorded_children),
                        seen_by_store[store_id], brands,
                    )
                    recently_done = recently_done | lead.walked
                    lead_deferred = lead.deferred
                # Rotate which brand goes FIRST, one step per run.
                #
                # The per-brand cursor keeps a brand fair to itself across
                # runs, but nothing kept the brands fair to each other: this
                # loop walked the CSV in fixed order and breaks on budget, so
                # a run that ran short always starved whichever brand is last.
                # Because the per-brand cursor resumes rather than restarts,
                # that brand is not starved to zero — it is SLOWED, completing
                # its cycle over more days than the first. For a record whose
                # whole claim is witnessed price history that is not cosmetic:
                # price-days are the currency of the maturity ladder, so a
                # permanently-second brand reaches each level later by
                # construction, and cross-brand comparisons then rest on
                # uneven observation cadence.
                #
                # Invisible with one brand configured, guaranteed with two.
                order = brand_order_for_run(brand_tokens, cursors, store_id)
                # The lead is handed on only if this brand actually gets its
                # turn; see advance_brand_order. Progress is either a walk or a
                # step of the brand's own category cursor — the second covers
                # the case where every category was already fresh, which is a
                # real turn taken and must not re-lead forever.
                lead_key = f"network|{store_id}|{order[0][1]}" if order else ""
                lead_cursor_before = cursors.get(lead_key)
                walks_before_lead = summary.walks
                for brand, token in order:
                    if client.is_throttled:
                        summary.aborted = True
                        break
                    if budget_spent(client, settings):
                        break
                    nav = build_nav(settings.root_nav_param, token)
                    # The root read only supplies the category list; each
                    # category walks under its own navParam, so there is no
                    # page here to carry forward.
                    total, dims, _ = await fetch_facets(
                        client, settings, nav, store_id, "ALL", page_size=1
                    )
                    categories = sorted(
                        (r for r in dims.get(CATEGORY_DIMENSION, [])
                         if r.get("count") and r["count"] > 0),
                        key=lambda r: (r.get("label") or r["token"]),
                    )
                    if not categories:
                        log.warning(
                            "Network tier: no category facets returned",
                            store_id=store_id, brand=brand,
                        )
                        continue
                    key = f"network|{store_id}|{token}"
                    start = cursors.get(key, 0) % len(categories)
                    picked = [categories[(start + i) % len(categories)] for i in range(min(per_run, len(categories)))]
                    log.info(
                        "Network tier categories this run",
                        store_id=store_id, brand=brand,
                        categories=[c.get("label") for c in picked],
                    )
                    # Advance the cursor only past categories we actually finish,
                    # not past the ones we merely selected. A run cut short by a
                    # 206 or the request budget otherwise skips its unwalked tail
                    # forever, so a slice of the online catalogue would never be
                    # seen. Counting completions lets the next online run resume
                    # exactly where this one stopped — the whole set gets covered.
                    completed = 0
                    for ref in picked:
                        if client.is_throttled:
                            summary.aborted = True
                            break
                        if budget_spent(client, settings):
                            # Stop before paying a facet read for a category
                            # this run cannot walk. completed stays put, so the
                            # cursor leaves it at the head of the next slice.
                            # Count everything still picked: a run that quietly
                            # stops planning would otherwise report the same
                            # summary as one that finished its slice.
                            #
                            # CATEGORIES, not walks — no walk has been planned
                            # for these yet, and a category resolves to one or
                            # many walks. Adding them to deferred_walks mixed
                            # two units in the number the admission-ceiling
                            # gate is judged on.
                            summary.deferred_categories += len(picked) - completed
                            break
                        child_nav = build_nav(nav, ref["token"])
                        child_label = f"{brand}/{ref.get('label') or ref['token']}"
                        if (ref.get("count") or 0) <= reachable_cap(settings):
                            # Under the cap: walk directly off the parent's count,
                            # no extra facet read needed.
                            walks = await resolve_walks(
                                client, settings, child_nav, child_label,
                                store_id, "ALL", total=ref.get("count"), dimensions={},
                                observed_totals=observed_totals,
                                recent_axis=recent_axis,
                                recorded_children=recorded_children,
                            )
                        else:
                            walks = await resolve_walks(
                                client, settings, child_nav, child_label, store_id, "ALL",
                                observed_totals=observed_totals,
                                recent_axis=recent_axis,
                                recorded_children=recorded_children,
                            )
                        # Forward progress. Anything completed inside the
                        # refresh window is already banked, so re-walking it
                        # spends the run without covering anything new — which
                        # is exactly how one oversized category came to be
                        # walked 157 times while its siblings were walked once.
                        planned = len(walks)
                        walks = [w for w in walks if w.nav_param not in recently_done]
                        if planned and not walks:
                            # Every piece is already fresh: the category IS
                            # covered for this pass. Advance past it.
                            log.info("Category already fresh — advancing",
                                     label=child_label, walks=planned)
                            completed += 1
                            continue
                        if len(walks) < planned:
                            log.info("Resuming a part-walked category",
                                     label=child_label, skipped=planned - len(walks),
                                     remaining=len(walks))
                        category_finished = True
                        for position, walk in enumerate(walks):
                            if client.is_throttled:
                                summary.aborted = True
                                category_finished = False
                                break
                            if not admits(client, settings, walk):
                                # Leaves `completed` un-advanced, so this
                                # category is the head of the next run's slice
                                # rather than being rotated past unwalked.
                                log.info(
                                    "Walk deferred — run cannot afford to finish it",
                                    label=walk.label, tier="ALL",
                                    estimate=walk_cost_estimate(walk, settings),
                                    used=client.request_count,
                                    ceiling=admission_ceiling(settings),
                                )
                                # Count every remaining walk, not just this
                                # one — the break abandons them all. Less any
                                # the lead already refused and counted: this is
                                # the same refusal reached a second time.
                                summary.deferred_walks += sum(
                                    1 for w in walks[position:]
                                    if w.nav_param not in lead_deferred
                                )
                                category_finished = False
                                break
                            walk_started = datetime.now(timezone.utc)
                            upserts, inserts = await walk_and_capture(
                                client, settings, walk, store_id, "ALL",
                                seen_by_store[store_id], brands,
                            )
                            await _record_walk(
                                settings, run_id, walk, store_id, "ALL", walk_started
                            )
                            summary.products += upserts
                            summary.snapshots += inserts
                            summary.walks += 1
                            if walk.truncated:
                                summary.truncated_walks.append(walk.label)
                            await _pause(settings)
                        if not category_finished:
                            # This category stopped mid-walk (admission defer
                            # or throttle) and the break below abandons every
                            # category still in the slice — structurally the
                            # same abandonment as the budget_spent path above,
                            # which does count them. Counting only there left
                            # the rest silently uncounted by EITHER counter,
                            # and this is the abandonment mode admission
                            # control actually produces, so the ~Sept 2 gate
                            # was under-reporting exactly what it measures.
                            # `- 1` because this category was planned and
                            # partly walked; its refused walks are already in
                            # deferred_walks. The two units are never summed,
                            # so this is not double counting.
                            summary.deferred_categories += max(
                                0, len(picked) - completed - 1
                            )
                            break
                        completed += 1
                    cursors[key] = (start + completed) % len(categories)
                if order and (
                    summary.walks > walks_before_lead
                    or cursors.get(lead_key) != lead_cursor_before
                ):
                    advance_brand_order(cursors, store_id, len(brand_tokens))
    except CircuitOpenError as e:
        # Reached run_browse uncaught before this, killing the process mid-run:
        # no cursor save, no cooldown written, no summary. Now it stops the run
        # the same way a throttle does.
        log.error("Circuit breaker opened — aborting run", error=str(e))
        summary.aborted = True
    finally:
        rotation.save_cursors(settings.browse_cursor_path, cursors)
        summary.aborted = summary.aborted or client.is_throttled
        await _record_run_end(settings, run_id, summary, client.request_count)
        log.info(
            "Browse complete",
            products=summary.products,
            snapshots=summary.snapshots,
            walks=summary.walks,
            # NOTE for anyone reading log history across 2026-08-28: the key
            # `deferred_categories` used to carry ROTATION skips and now
            # carries budget deferrals at planning time. Rotation moved to
            # `rotation_skipped`. Lines either side of that date mix two
            # quantities under one key with no marker in the logs themselves.
            rotation_skipped=summary.skipped_walks or None,
            deferred_walks=summary.deferred_walks or None,
            deferred_categories=summary.deferred_categories or None,
            truncated=summary.truncated_walks or None,
            requests_used=client.request_count,
            throttled=client.is_throttled,
            failures=client.failures or None,
        )
        if owns_client:
            await client.close()

    return summary
