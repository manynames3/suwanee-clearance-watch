"""Walk planning for the brand browse: what to walk, in what order, at what cost.

Pure planning — no HTTP, no database. Splits catalogue nodes into walks by facet
and price band, rotates brand and shelf coverage across runs, and decides what a
run can afford. The fetching of those walks lives in `fetcher`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hd.config import Settings
from hd.http.client import HDClient
from zoneinfo import ZoneInfo

from hd.logging import get_logger

log = get_logger("pipeline.browse")


CATEGORY_DIMENSION = "Category"


PRICE_DIMENSION = "Price"


@dataclass
class Walk:
    """One fully-paginatable navParam result set."""

    nav_param: str
    label: str
    total: int
    truncated: bool = False  # True when splitting could not get under the cap
    both_ends: bool = False  # walk both price ends instead of splitting by facet
    all_brands: bool = False  # capture every brand on the page; the node, not the brand list, is the scope
    # The facet read for this node, reusable as page 0 when the walk covers the
    # same navParam. Excluded from equality so a primed walk still compares
    # equal to the plan that produced it.
    primed: dict | None = field(default=None, compare=False, repr=False)
    # Runtime outcome, filled by walk_and_capture and read by walk_status.
    # Excluded from equality for the same reason as primed: a walked plan
    # still compares equal to the plan that produced it.
    observed_ids: int | None = field(default=None, compare=False)
    live_total: int | None = field(default=None, compare=False)
    # A mid-walk stop that was not the natural end of the results: a fetch
    # error, an unusable page, or a throttle. Distinct from `truncated`,
    # which is a planning-time judgment; walk_status folds both together.
    cut: bool = field(default=False, compare=False)
    # The split this walk is a piece of: the parent's navParam and the
    # dimension it was split on. Both None for a walk that IS its own node.
    # Persisted to walk_coverage so the next run can read which axis the
    # record is keyed on instead of guessing it from navParam shape — see the
    # note above plan_walks. Compared, unlike the runtime fields below: two
    # plans that split the same node on different axes are different plans.
    split_parent: str | None = None
    split_axis: str | None = None


@dataclass
class BrowseSummary:
    products: int = 0
    snapshots: int = 0
    walks: int = 0
    truncated_walks: list[str] = field(default_factory=list)
    aborted: bool = False
    # Categories deliberately deferred to a later run by shelf rotation. Not a
    # failure, but it does mean this run did not see the whole shelf.
    skipped_walks: int = 0
    # Walks not started because the run could not afford to finish them. Also
    # not a failure — the alternative is a truncated walk, which is worse — but
    # kept separate from skipped_walks: rotation defers on a fixed schedule,
    # this defers on budget, and only one of them says the run ran short.
    #
    # WALKS ONLY. Categories deferred before any walk was planned for them go
    # to deferred_categories below. The two cannot be summed — one category
    # resolves to one or many walks — and this counter is the instrument the
    # admission-ceiling experiment is judged on, so a mixed unit here lands on
    # a gate decision.
    deferred_walks: int = 0
    # Categories dropped at PLANNING time, before their walks existed, because
    # the run had already spent its budget. Same cause as deferred_walks, a
    # different unit.
    deferred_categories: int = 0


def brand_order_cursor_key(store_id: str, tier: str = "network") -> str:
    """Cursor key for which brand leads this run on one tier.

    Keyed per TIER as well as per store. The shelf and network tiers each
    rotate their own brand order, and a shelf-only run (two of every six on
    the live schedule) must not advance the network tier's rotation: with one
    shared key it would, and a both-tiers run would advance it twice — which
    is the uneven cadence the rotation exists to prevent. Today's config
    masks it (a single-brand shelf list never advances), so the shared key was
    a latent defect, not a live one.
    """
    return f"{tier}|{store_id}|brand-order"


def brand_order_for_run(
    brand_tokens: list[tuple[str, str]],
    cursors: dict[str, int],
    store_id: str,
    tier: str = "network",
) -> list[tuple[str, str]]:
    """`brand_tokens` rotated so a different brand leads this run.

    READ-ONLY. The cursor moves in `advance_brand_order`, called once the
    leading brand has actually had its turn — the same advance-on-progress
    rule the category cursor below already follows, and for the same reason.
    With one brand this is the identity and costs nothing.

    Deliberately keyed per store and per tier: stores are walked in their own
    loop and each one exhausts the shared budget independently, and each tier
    rotates on its own schedule.
    """
    if len(brand_tokens) < 2:
        return list(brand_tokens)
    key = brand_order_cursor_key(store_id, tier)
    offset = cursors.get(key, 0) % len(brand_tokens)
    return list(brand_tokens[offset:]) + list(brand_tokens[:offset])


def advance_brand_order(
    cursors: dict[str, int],
    store_id: str,
    brand_count: int,
    tier: str = "network",
) -> None:
    """Hand the lead to the next brand — but only once this one has had it.

    Rotation used to advance the moment the order was READ, which spends a
    brand's turn on a run that walks nothing. A run throttled at its first
    request, or an extra run outside the schedule, consumed a turn all the
    same — and because rotation is modular, a single spurious step INVERTS the
    parity of every run after it. With two brands that is not a small
    unfairness: it is one brand leading four runs out of five while the other
    leads none, and the trailing brand only ever gets the budget the leader
    leaves behind.

    It bites hardest where the brands are unequal in size. A brand whose whole
    category set fits inside one run's budget is complete whenever it leads and
    nearly invisible whenever it does not, so the rotation decides its coverage
    rather than the budget does. Measured over two weeks on a two-brand install,
    the smaller brand's daily coverage of its own category set was bimodal on
    exactly this axis: leading twice a day covered 21-25 of its 26 nodes,
    leading once covered 18-24, leading not at all covered 13-15.

    So the caller advances only after the leading brand has made progress —
    walked something, or moved its own category cursor. Anything else leaves
    the lead where it is, and the brand that was owed a turn gets it next run.
    """
    if brand_count < 2:
        return
    key = brand_order_cursor_key(store_id, tier)
    cursors[key] = (cursors.get(key, 0) + 1) % brand_count


def build_nav(root: str, *tokens: str) -> str:
    """Compose a navParam from the root and facet tokens."""
    parts = [root] + [t for t in tokens if t]
    return "Z".join(parts)


def reachable_cap(settings: Settings) -> int:
    """Largest result set the API allows walking end to end."""
    return settings.api_max_start_index + settings.page_size


def both_ends_cap(settings: Settings) -> int:
    """Largest result set two price-ordered ends can cover with a safe overlap.

    PRICE ASC and PRICE DESC each reach `reachable_cap` items, so their union
    spans up to 2*cap — minus a margin held back so the seam (where the
    tie-break is not mirrored between the two orderings) always overlaps by
    several pages rather than meeting at a knife edge.
    """
    margin = settings.both_ends_min_overlap_pages * settings.page_size
    return 2 * reachable_cap(settings) - margin


def walk_cost_estimate(walk: Walk, settings: Settings) -> int:
    """Requests this walk will need, before it is started.

    Bounded by what the walk can actually reach, not by the node's total: the
    API refuses startIndex past api_max_start_index, so a walk over the cap
    stops at the reachable head rather than paging the whole node.

    The trailing +1 is a flat safety margin, NOT the facet read — `admits` is
    called after `resolve_walks` has already paid for that, so it is in
    `client.request_count` before this estimate is ever compared. Keep the
    margin: it is what stops a walk that lands exactly on the ceiling from
    being the request that trips the quota. The primed discount is separate
    and real — a walk that reuses page 0 genuinely issues one request fewer —
    so both together still leave the margin intact.

    KNOWN LIMIT, deliberately accepted. For a single walk this is an upper
    bound only while the node's reported total is accurate. `_page_direction`
    keeps paging while pages come back full, so a node that UNDERSTATES its
    total self-extends past this estimate — up to the structural ceiling of
    `per_direction` pages, never beyond. Sizing every walk at that ceiling
    instead would be airtight but would admit only ~7 walks per run against a
    237 ceiling, where runs currently complete 21-47; it would cost far more
    coverage than the overshoot it prevents. The measured basis for accepting
    it: across 118 complete walks in `walk_coverage`, observed == expected
    every time, so no single walk has yet self-extended at all. The ceiling
    keeps margin below the real quota stop to absorb one such overshoot.

    Both-ends is different and is sized at the worst case, because there the
    worst case is routine rather than hypothetical — see below.
    """
    page_size = max(1, settings.page_size)
    total = walk.total or 0
    per_direction = settings.api_max_start_index // page_size + 1
    if walk.both_ends:
        # Worst case, and it is reached routinely. ASC always runs to the API
        # ceiling (a both-ends node is by definition over reachable_cap). DESC
        # stops early only when the union reaches the live total — so whenever
        # the union falls SHORT, DESC also runs to the ceiling. That shortfall
        # is not exotic: it is the "Both-ends coverage short" branch below, and
        # on this catalogue it fires about half the time (seam duplicates, or a
        # total that overstates the distinct itemIds the node will return).
        #
        # Sizing this from the node's item count instead under-estimates by up
        # to a full direction — worst at the BOTTOM of the band, where a 745
        # item node looks like 32 pages and can cost 62. Over-estimating only
        # defers a walk to the next run; under-estimating starts a walk that
        # cannot finish, which is the exact failure this function exists to
        # prevent.
        pages = 2 * per_direction
    else:
        items = min(total, reachable_cap(settings))
        pages = math.ceil(items / page_size)
    # A primed page 0 is only reused on the default BEST_MATCH ordering
    # (_page_direction: `page == 0 and order_by is None`). Both-ends walks
    # always pass a price ordering, so they never consume it and must not be
    # discounted for it.
    discount = 1 if (walk.primed and not walk.both_ends) else 0
    return max(1, pages) + 1 - discount


def admission_ceiling(settings: Settings) -> int:
    """Request count past which no new walk is started. 0 means "no ceiling".

    The tighter of the two positive budgets, never just the explicit one: a
    caller that lowers `browse_request_budget` for a bounded run (the setup
    wizard's install check does exactly this) would otherwise keep a much
    larger configured ceiling, admit walks it cannot pay for, and have them cut
    by budget exhaustion — writing truncated coverage rows out of a health
    check.
    """
    candidates = [v for v in (settings.browse_walk_admission_ceiling,
                              settings.browse_request_budget) if v > 0]
    return min(candidates) if candidates else 0


def budget_spent(client: HDClient, settings: Settings) -> bool:
    """Whether the run has reached the point where it should stop planning too.

    `admits` gates walks, but the planning reads that PRECEDE a walk —
    `fetch_facets` and the facet reads inside `resolve_walks` — are requests
    like any other and count against the same quota. Without this the outer
    loops keep resolving nodes they will then immediately defer, spending the
    very margin the ceiling was bought with, and a quota stop landing on a
    facet read aborts the run just as hard as one landing on a page.
    """
    ceiling = admission_ceiling(settings)
    return ceiling > 0 and client.request_count >= ceiling


def admits(client: HDClient, settings: Settings, walk: Walk) -> bool:
    """Whether this run can still afford to start `walk` and finish it.

    Starting a walk we cannot finish is the expensive mistake: the quota stop
    cuts it mid-page and the coverage row lands "truncated", which is worth
    less than nothing to anything reasoning from absence. Deferring instead
    leaves the walk for a run with room, and writes no row at all.

    The starvation guard covers a walk too large for any single run: deferring
    it every time would mean never walking it. Such a walk is admitted while
    the run still holds at least half its ceiling, so it gets the deepest pass
    available and truncates once rather than never running. With the API's own
    startIndex ceiling bounding every walk to ~2 caps, this branch is currently
    unreachable in practice; it exists so a small ceiling cannot starve a node.
    """
    ceiling = admission_ceiling(settings)
    if ceiling <= 0:
        # 0 means "no budget" everywhere else in this config (see
        # request_budget), so it has to mean "no ceiling" here too. Without
        # this the arithmetic below inverts the intent: remaining goes
        # negative after the first walk and every later one is deferred.
        return True
    remaining = ceiling - client.request_count
    est = walk_cost_estimate(walk, settings)
    if est <= remaining:
        return True
    if est > ceiling:
        return remaining >= ceiling // 2
    return False


# A price band is the only refinement in the Price dimension that partitions by
# price. Home Depot also files merchandising buckets there ("Special Values"),
# which OVERLAP the bands rather than extending them.
#
# Matched on a currency amount ANYWHERE in the label, not a leading "$". The
# top band is open-ended and reads "Over $5000" — it appears in 940 of the
# 5,652 responses on file and holds up to 27 items in a single node. A
# leading-"$" test called it merchandising and would have dropped it, hiding
# every item above $5,000 with no error and no coverage row to show for it.
# Searching for the amount also errs the safe way: an unrecognised bucket that
# happens to carry a price is walked rather than discarded.
PRICE_BAND_LABEL = re.compile(r"\$\s*[\d,]")


# Non-band Price refinements known by measurement to be contained by the bands.
# Anything else that reaches the drop is an assumption, not a finding, and says
# so in the log — see the classification note in price_partition.
KNOWN_CONTAINED_PRICE_LABELS = frozenset({"Special Values"})


def _count(refinement: dict) -> int:
    """A refinement's count, treating an absent one as zero.

    `parse_dimensions` deliberately preserves a missing recordCount as None.
    Zero is the safe reading here: it can only shrink the bands' total and so
    only ever pushes this function toward walking everything.
    """
    return refinement.get("count") or 0


def price_partition(
    refinements: list[dict], total: int, label: str = "",
) -> list[dict]:
    """The Price refinements to walk: the bands, minus what they already hold.

    The Price dimension is not a partition. Alongside the price bands it
    carries merchandising buckets — "Special Values" is the only one this
    catalog serves, across 5,652 saved responses — and those are a SUBSET of
    the bands, not a slice beside them. Walking the dimension whole therefore
    reads the same items twice. Measured on two nodes: the bands alone already
    summed to 1,748 against a node total of 1,709, and to 1,807 against 1,802,
    while the merchandising bucket added a further 593 and 679 reads that could
    reach nothing new — and inserted zero snapshots, because the ids were
    already in this store's seen set.

    **What the check proves, stated exactly: the COUNTS balance.** It does not
    prove set containment — two different sets can be the same size — and the
    slack is sometimes one item. Across the 1,510 over-cap responses on file
    that carry both bands and a merchandising bucket, the median margin is +54
    but the minimum is **+1** (MILWAUKEE/Outdoors: total 812, bands 813). At
    +1, a single item outside every band makes the arithmetic balance anyway.
    The check still earns its place — it fires on 31 real responses — but it is
    a counting argument, not a proof, and it is the reason the drop is limited
    to buckets already measured to be contained.

    **Classification is by exclusion, which is an assumption.** Anything
    carrying no currency amount is treated as contained. That is true of
    "Special Values" and is not a general law: an ADDITIVE refinement with no
    price in its label — "Free Delivery", "Clearance" — would be dropped
    silently in the 99.4% of nodes where the counts balance. The band regex
    errs safe for the reverse case (an unknown bucket that carries a price is
    walked), so this is the exposed direction, and any label outside
    KNOWN_CONTAINED_PRICE_LABELS is logged as a warning rather than waved
    through. Widen that set only after measuring the new bucket.

    A dimension with no bands at all is returned untouched: then the
    merchandising buckets are the only split on offer.
    """
    bands: list[dict] = []
    others: list[dict] = []
    for ref in refinements:
        is_band = bool(PRICE_BAND_LABEL.search(ref.get("label") or ""))
        (bands if is_band else others).append(ref)
    if not bands or not others:
        return refinements
    covered = sum(_count(r) for r in bands)
    if covered < total:
        log.info(
            "Price bands do not cover the node — walking every refinement",
            label=label, total=total, covered=covered,
            extra=[r.get("label") for r in others],
        )
        return refinements
    strangers = [r.get("label") for r in others
                 if (r.get("label") or "") not in KNOWN_CONTAINED_PRICE_LABELS]
    if strangers:
        log.warning(
            "Unknown Price refinement assumed contained by the bands — verify "
            "it is not additive before trusting this drop",
            label=label, unknown=strangers, total=total, covered=covered,
        )
    log.info(
        "Dropping price refinements the bands already contain",
        label=label, total=total, covered=covered,
        dropped=[r.get("label") for r in others],
        saved_items=sum(_count(r) for r in others),
    )
    return bands


# THE SPLIT AXIS IS NOT STABLE PER NODE, AND THE RECORD NOW SAYS WHICH ONE IT USED.
#
# The Category dimension for a given node comes back carrying its children on
# one run and carrying only the node itself on the next — and the node's own
# token is filtered out as already-in-nav, leaving nothing to split on, so the
# same node routed "category-split" one run and "price-split" a few hours
# later. The two axes produce different child navParams, the freshness skip is
# keyed on navParam, and nothing recognised the second pass as a repeat: the
# node was re-read whole, at a measured ~48 requests/day.
#
# ONE node, not a general property of the dimension. Counted over the whole
# routing log: MILWAUKEE/Tools/Hand Tools flipped 21 times in 37 runs, and of
# the 23 other nodes routed more than once, not one ever changed branch. The
# fix is sized to that.
#
# WHAT SHIPPED: the split is RECORDED. Every child a split produces carries its
# parent's navParam and the axis it was split on into walk_coverage, and
# coverage_memory reads the most recent axis per parent back out. A node split
# inside the freshness window is split on the SAME axis again, so its children
# carry the navParams the record already holds and the existing per-navParam
# freshness skip does the rest. The preference lapses as soon as no child of
# that split is fresh any more — the node is then due a full sweep, the skip
# would skip nothing whichever axis were picked, and the ordinary
# category-first order chooses again. Without that clause the preference never
# lapses at all: runs land every ~4h against a 20h window, so the node would be
# pinned for good to whichever axis it happened to start on.
#
# Deliberately NOT a roll-up. Nothing here concludes that a parent is covered:
# every skip is still the one the scanner already trusted — this exact
# navParam completed inside the window. All the recorded axis does is stop the
# planner from asking the same question in a vocabulary the record cannot
# answer. That matters, because rolling a completed split UP to the node it
# split was tried as the fix and WITHDRAWN: summing a node's recently-completed
# children silently assumes they partition it, and they do not. The review that
# withdrew it replayed it over 43 real runs and recorded 5 covers where neither
# axis had covered the node, the worst skipping ~220 items behind a single INFO
# line. Re-replaying it here reproduced that failure mode but not that count —
# a harness that judges one decision per run against the itemIds the saved
# pages actually returned puts it at 1 unsound skip in 6 firings over
# 2026-08-31 to 09-03, which is a different window and a different question, so
# treat the 5 as the earlier review's number and not as a reproduction. It also
# needed the parent's own items_expected as a denominator,
# which exists only for a node whose split once FAILED and can never be
# refreshed, since a successful split writes no row for the parent. Recording
# the axis needs neither a sum nor a denominator.
#
# MEASURED BEFORE TRUSTING IT, because the whole saving rests on the two axes
# being the same set of items and nobody had checked. Over 2026-09-02/03 the
# category axis (17 children) saw 1,725 distinct itemIds and the price axis
# (20 bands) saw 1,736, sharing 1,722: 3 category-only and 14 price-only. The
# same-node-across-a-day churn baseline over the same corpus is 1.45%, so the
# 0.99% the axes disagree by is smaller than the noise the catalogue makes on
# its own. Sticking to one axis for one window therefore costs nothing
# measurable.
#
# Both axes stay in use, but be honest about how much less. In the 12-run
# replay the preference lapsed on 3 runs, and only 1 of those 3 fell on a run
# where the Category dimension had come back at all, so the planner chose
# category once in 12 where today it chooses it 5 times in 12 — the same 5 runs
# on which the dimension was available at all. That is the trade: the minority
# axis is exercised about a fifth as often, against a measured 0.99%
# disagreement between what the two axes see. If that disagreement ever grows,
# this is the line to revisit.
#
# What the disagreement is made of, since this is where the change could lose
# something. Of the 3 items the category axis saw and the price axis did not,
# two are demonstrably price-reachable: the $160.86 one turns up under a band
# on 3 saved runs (under $20 - $30, not the band its displayed price implies —
# worth its own look) and the $416.94 one under its own band on 2 of that
# band's 8 saved runs. The third, at $149.97, appears nowhere under any band in
# the corpus — but it first enters the record at all on 2026-09-03 04:07, and
# its band was last walked 2026-09-02 20:07, so no price sweep has yet had the
# chance to see it. Absence of opportunity, not absence of access.
#
# NOT VERIFIED IN PRODUCTION, and the kill condition is the one the admission
# ceiling already uses: if daily requests have not fallen and the
# complete:truncated ratio in walk_coverage has not held after a week, revert
# rather than tune. The replay says ~38 requests/day on this node over the 12
# runs it covers, against the ~48/day the flip was measured to cost; that is a
# reconstruction from saved pages and recorded rows, not a live reading.
#
# THE SAVING IS NET, NOT PER RUN, and that is worth knowing before reading the
# first day of logs as a regression. Holding one axis for a window concentrates
# a full sweep into one run where the flipping behaviour sometimes spread it
# over two, so in the same replay 4 of the 12 runs spend MORE on this node than
# they actually did — worst +75 — while the twelve together spend 125 fewer.
# The worst single-run figure it produces for this node is 120 requests against
# an observed actual peak of 103. Read that against the ceiling actually in
# force, not the code default: BROWSE_WALK_ADMISSION_CEILING is set to 237, so
# admission_ceiling() is 237 rather than browse_request_budget's 280, and one
# node spiking to 120 is about half of it. BROWSE_NETWORK_CATEGORIES_PER_RUN is
# set to 12, so up to twelve top-level categories compete for that same 237.
#
# And there is no headroom to absorb it. Over the 7 days to 2026-09-03 the
# busiest run used 236 requests against that ceiling of 237, and the run of
# 2026-09-03 00:00 ET spent 233 and deferred 9 walks and 5 categories. Roughly
# a third of runs already defer something. So this does not land on a budget
# with room in it; it lands on one that is already binding.
#
# That is a live risk and not a theoretical one, because of what a deferral
# does in the network-tier loop below. Each band costs single digits, far under
# the ceiling, so the starvation clause in admits() never engages for this node
# and it is the ordinary "can I afford this walk" test that eventually fails.
# When it does, the tail of this node's own children defers AND category_finished
# goes False, which leaves `completed` un-advanced and breaks out of the slice —
# abandoning every other category the run had picked. So a spike here does not
# only risk this node; it can starve whatever was scheduled behind it.
#
# WHAT THIS DOES NOT FIX. A node-level rule — "skip the other axis once every
# child of the recorded split is complete" — has still never been able to fire
# here, because no run has yet had every child of either axis complete at the
# same time. But be careful with the reason, because the first post-merge run
# corrected it. The original claim here was that both axes contain a child that
# has NEVER completed: Hand Tools/$400 - $500 0 in 10 attempts and
# Hand Tools/Measuring Tools 0 in 8, each short by exactly one item. On
# 2026-09-03 16:00 ET that band claimed 44 and returned all 44, so it now
# stands at 1 complete in 11 and the "never" was wrong. Measuring Tools is
# still 0 in 8.
#
# So the one-short behaviour is INTERMITTENT, not a fixed property of these
# nodes, and a node-level rule is blocked by a coincidence that has not yet
# happened rather than by one that cannot. Reading the saved pages back shows
# the shape of it. Across all 8 saved runs of $400 - $500 the band claims N and pagination
# returns N-1, with N rising 41, 40, 42, 42, 43, 43, 43, 43 as the catalogue
# grows — page 0 gives 24, page 1 gives the rest, page 2 gives none, so the
# walk reaches its natural end every time and is still one short.
#
# Do not go looking for a bad count, do not go looking for one phantom item,
# and do not go looking at the paging. The count is right: across the 4 runs
# that claim 43 the union of what they returned is exactly 43, so all 43 exist.
# Each of those runs returned 42 and omitted ONE — and not the same one:
# 325682043 in the first, 317097247 in the other three.
#
# The omission is not at a page boundary. When they are returned at all, those
# two sit at position 14 and position 36 of 42, mid-page in both cases; the
# pages stay 24 then 18 either way because offset paging just closes the gap.
# So a single traversal is served a result set of 42 out of a true 43, with an
# arbitrary member absent, and pages that set faithfully. The defect is in what
# one request is willing to return, not in how the walk pages it.
#
# walk_status is right to call the walk truncated and the node can never be
# declared covered. That is the pre-existing phantom-item defect, it is worth
# its own look, and it is why this fix is per-child rather than per-node.
#
# ALSO UNRESOLVED: the degraded read. Three nodes whose true sizes are ~1,715,
# ~1,800 and ~2,240 all intermittently report a total in the narrow band
# 1,473-1,494 — eighteen such readings in the log, with 1494 landing five
# separate times on a node that otherwise reads 1,794-1,811. A real count does
# not hit the same integer five times while its ordinary value drifts every
# run. The likeliest reading is that the API sometimes answers with a result
# set for a different scope than the navParam asked for. Recording the axis
# does not diagnose that, though it does make the planner stop reacting to it:
# the flips of 2026-09-02 and 09-03 all happened at full totals of 1,723-1,726,
# so the degraded read is a separate fault and not the cause of the flip.
# `observed_totals` already holds each node's own high-water and is already
# threaded into this function; declining to route off a page-0 total that comes
# back at a fraction of it costs no schema change. Understand the degraded read
# before paying for anything more.
def plan_walks(
    nav_param: str,
    label: str,
    total: int | None,
    dimensions: dict[str, list[dict]],
    settings: Settings,
    depth: int = 0,
    price_split_used: bool = False,
    observed_totals: dict[str, int] | None = None,
    recent_axis: dict[str, str] | None = None,
    recorded_children: dict[str, list[tuple[str, str, int]]] | None = None,
) -> tuple[list[Walk], list[tuple[str, str]]]:
    """Split one result set into walkable pieces using its own facets.

    Returns (walks, need_facets) where need_facets lists (nav_param, label)
    nodes that are over the cap and need their own facet fetch to split
    further. Pure apart from one log line per routing decision: no network or
    database I/O — the caller fetches facets and re-plans.

    `recent_axis` maps a node's navParam to the axis the RECORD says it was
    last split on inside the freshness window (see coverage_memory). A node
    named there is split on that axis again when this run's facets still
    support it, so its children carry the navParams the record already holds
    and the caller's per-navParam freshness skip can recognise them.

    `recorded_children` maps the same navParams to the children that split
    produced last time — (navParam, label, expected) per child, every status.
    When this run's facets cannot build the recorded axis, the split is
    REBUILT FROM THE RECORD rather than taken on the other axis. Measured
    reason: run 70 (2026-09-06 04:03Z) held a category preference for
    MILWAUKEE/Tools/Hand Tools, HD served no Category dimension on that read,
    the planner fell through to price, and 20 price children none of which
    the freshness skip knew re-read a node whose 16 category children had
    completed 3h52m earlier — 104 of the run's 135 requests for zero new
    coverage. The rebuilt children are exactly the navParams the record
    holds, so the skip recognises them and only the stale ones walk.

    With no record to rebuild from, the ordinary category-then-price order
    applies, and the substitution is logged as a warning rather than passing
    silently. Preference only — it never suppresses a walk the facets would
    have produced.
    """
    if total is None or total <= 0:
        return [], []
    cap = reachable_cap(settings)
    band_cap = both_ends_cap(settings)

    def _route(branch: str) -> None:
        # Split parents never write a page 0, so this line is the archive's
        # only record of a parent node's total and route — and the only thing
        # that shows a node drifting into the both-ends band
        # (cap < total <= band_cap).
        # navParam as well as label: `label` falls back to the raw facet token
        # when HD omits one, so the same node can carry two labels across runs
        # and a routing history keyed on it cannot be trusted to be one node's.
        # This line is, by the comment above, the only durable record a split
        # parent leaves — attributing it needs the stable identifier.
        log.info("Walk routing", label=label, nav_param=nav_param, total=total,
                 branch=branch, cap=cap, band_cap=band_cap, depth=depth)

    if total <= cap:
        _route("single")
        return [Walk(nav_param, label, total)], []
    if settings.both_ends_paging and total <= band_cap:
        # Too big for one walk, small enough for two price ends to cover with
        # margin — walk both ends in one go instead of splitting by facet.
        _route("both-ends")
        return [Walk(nav_param, label, total, both_ends=True)], []
    if depth >= settings.browse_max_split_depth:
        log.warning(
            "Facet split depth exhausted — tail beyond API ceiling is unreachable",
            label=label, total=total, cap=cap,
        )
        _route("truncated-depth")
        return [Walk(nav_param, label, total, truncated=True)], []

    existing_tokens = set(nav_param.split("Z"))
    categories = [
        r for r in dimensions.get(CATEGORY_DIMENSION, [])
        if r.get("count") and r["count"] > 0 and r["token"] not in existing_tokens
    ]

    def _price_walks() -> list[Walk] | None:
        """The price-split branch, or None when Price cannot partition the node."""
        if price_split_used:
            return None
        prices = price_partition(
            [r for r in dimensions.get(PRICE_DIMENSION, [])
             if r.get("count") and r["count"] > 0],
            total, label=label,
        )
        if not prices:
            return None
        out: list[Walk] = []
        for ref in prices:
            child_nav = build_nav(nav_param, ref["token"])
            child_label = f"{label}/{ref.get('label') or ref['token']}"
            truncated = ref["count"] > cap
            if truncated:
                log.warning(
                    "Price bucket still over API ceiling — walking reachable head only",
                    label=child_label, total=ref["count"], cap=cap,
                )
            out.append(Walk(child_nav, child_label, ref["count"], truncated=truncated,
                            split_parent=nav_param, split_axis="price"))
        return out

    def _from_record(axis: str) -> list[Walk] | None:
        """The recorded split rebuilt as walks, or None when there is none.

        Only leaves are on record — a child that was itself split further
        left no walk row of its own — so a record whose direct children are
        partly split parents is incomplete and is refused: rebuilding it
        would silently drop a sub-tree, which is worse than the re-read.
        """
        recorded = (recorded_children or {}).get(nav_param) or []
        if not recorded:
            return None
        recorded_navs = {nav for nav, _, _ in recorded}
        # A recorded leaf under this node that is NOT a direct child means a
        # direct child was split further; that child has no row and the
        # record cannot stand in for the facets.
        prefix = nav_param + "Z"
        for nav in recorded_navs:
            tail = nav[len(prefix):] if nav.startswith(prefix) else ""
            if not tail or "Z" in tail:
                log.warning(
                    "Recorded split is incomplete — not rebuilding from it",
                    label=label, nav_param=nav_param, axis=axis, child=nav,
                )
                return None
        out: list[Walk] = []
        for child_nav, child_label, expected in recorded:
            count = max(int(expected or 0), (observed_totals or {}).get(child_nav, 0))
            out.append(Walk(child_nav, child_label, count, truncated=count > cap,
                            split_parent=nav_param, split_axis=axis))
        log.info("Rebuilding the recorded split — this run's facets cannot",
                 label=label, nav_param=nav_param, axis=axis, children=len(out))
        return out

    def _fallback_warning(axis: str, taking: str) -> None:
        log.warning(
            "Record axis unavailable this run — falling back to the other axis",
            label=label, nav_param=nav_param, recorded_axis=axis, taking=taking,
            categories_available=len(categories),
        )

    # The record's axis wins over the category-first default. When this run's
    # facets can build it, they do; when they cannot, the record's own
    # children stand in (see _from_record); and only with neither does the
    # ordinary category-then-price order apply — loudly.
    preferred = (recent_axis or {}).get(nav_param)
    price_tried = False
    price_walks: list[Walk] | None = None
    if preferred == "price":
        price_walks = _price_walks()
        price_tried = True
        if price_walks is not None:
            log.info("Splitting on the axis the record holds",
                     label=label, nav_param=nav_param, axis="price",
                     children=len(price_walks), categories_available=len(categories))
            _route("price-split")
            return price_walks, []
        rebuilt = _from_record("price")
        if rebuilt is not None:
            _route("price-split")
            return rebuilt, []
        if categories:
            _fallback_warning("price", "category")

    if categories:
        walks: list[Walk] = []
        need: list[tuple[str, str]] = []
        for ref in categories:
            child_nav = build_nav(nav_param, ref["token"])
            child_label = f"{label}/{ref.get('label') or ref['token']}"
            # The parent's recordCount is a claim, and it is sometimes wrong by
            # multiples: three Tools children were advertised at 603/453/381
            # and reported 2197/1720/1798 at their own page 0 — under the cap
            # by the claim, 3x over it in fact. Routed as single walks, they
            # ran to the API ceiling, covered ~34%, and the cursor moved on as
            # if the category were done. Prefer whatever a walk has actually
            # SEEN at page 0 over what the parent says about it.
            claimed = ref["count"]
            count = max(claimed, (observed_totals or {}).get(child_nav, 0))
            if count > claimed:
                log.info("Node count corrected from observation",
                         label=child_label, claimed=claimed, observed=count)
            if count <= cap:
                walks.append(Walk(child_nav, child_label, count,
                                  split_parent=nav_param, split_axis="category"))
            else:
                need.append((child_nav, child_label))
        _route("category-split")
        return walks, need

    if preferred == "category":
        rebuilt = _from_record("category")
        if rebuilt is not None:
            _route("category-split")
            return rebuilt, []
        _fallback_warning("category", "price")

    if not price_tried:
        price_walks = _price_walks()
    if price_walks is not None:
        _route("price-split")
        return price_walks, []

    log.warning(
        "No facet available to split oversized set — walking reachable head only",
        label=label, total=total, cap=cap,
    )
    _route("truncated-no-facet")
    return [Walk(nav_param, label, total, truncated=True)], []


# Home Depot's own clock. Matches setup_schedule, which builds the cron slots
# from Eastern wall time for the same reason.
SCHEDULE_TZ = ZoneInfo("America/New_York")


def parse_et_hours(csv: str) -> set[int]:
    """Parse a CSV of Eastern-time hours (0–23) into a set; ignores junk."""
    hours: set[int] = set()
    for part in str(csv or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.add(int(part))
    return hours


def current_et_hour(now: datetime | None = None) -> int:
    """The current hour on Home Depot's clock — the same clock the schedule uses."""
    return (now or datetime.now(timezone.utc)).astimezone(SCHEDULE_TZ).hour


def full_shelf_hours(settings: Settings) -> set[int]:
    return parse_et_hours(getattr(settings, "browse_full_shelf_hours_et", ""))


def effective_shelf_fraction(settings: Settings, now: datetime | None = None) -> float:
    """1.0 on a designated full-walk hour, the configured slice otherwise.

    Read once per run rather than per store, so a run that starts at 03:59 and
    crosses into 04:00 keeps the fraction it began with instead of changing
    behaviour halfway through.
    """
    hours = full_shelf_hours(settings)
    if not hours:
        return settings.browse_shelf_fraction
    now = (now or datetime.now(timezone.utc)).astimezone(SCHEDULE_TZ)
    return 1.0 if now.hour in hours else settings.browse_shelf_fraction


def network_lead_for_run(
    settings: Settings, tiers: tuple[str, ...], now: datetime | None = None,
) -> list[tuple[str, str, tuple[str, ...]]]:
    """(brand, facet token, label path) for each node this run walks first.

    Empty means no lead: the setting is off, this is not a network run, or the
    run did not start in a lead hour. Read once per run, like
    `effective_shelf_fraction` and for the same reason — a run that crosses
    the hour keeps the behaviour it began with.

    The brand comes back in the BRAND_TOKENS spelling, because that is the
    spelling the rotation labels its walks with and the two must agree.
    """
    if "network" not in tiers:
        return []
    hours = parse_et_hours(settings.browse_network_lead_hours_et)
    if not hours or current_et_hour(now) not in hours:
        return []
    tokens = {b.upper(): (b, t) for b, t in settings.brand_token_list}
    leads: list[tuple[str, str, tuple[str, ...]]] = []
    for brand, path in settings.browse_network_lead_list:
        known = tokens.get(brand.upper())
        if known:
            leads.append((known[0], known[1], path))
    return leads


def rotate_shelf_walks(
    walks: list[Walk],
    cursors: dict[str, int],
    store_id: str,
    token: str,
    settings: Settings,
    fraction: float | None = None,
) -> tuple[list[Walk], int]:
    """This run's slice of the shelf categories. Returns (picked, skipped).

    The shelf tier re-paginates every category on every run — measured at 51
    walks and 154 page requests, six times a day — while most categories change
    far more slowly than that. Walking a slice per run trades coverage latency
    for request volume: with a fraction of 0.5 a category is revisited every
    other run instead of every run.

    Ordering is by label so the cursor means the same thing between runs even
    as the catalog shifts underneath it.
    """
    fraction = settings.browse_shelf_fraction if fraction is None else fraction
    if fraction >= 1.0 or len(walks) <= 1:
        return walks, 0
    ordered = sorted(walks, key=lambda w: w.label)
    per_run = max(1, math.ceil(len(ordered) * fraction))
    key = f"shelf|{store_id}|{token}"
    start = cursors.get(key, 0) % len(ordered)
    picked = [ordered[(start + i) % len(ordered)] for i in range(per_run)]
    cursors[key] = (start + per_run) % len(ordered)
    return picked, len(ordered) - len(picked)


def _prime(walks: list[Walk], nav_param: str, raw: dict | None) -> None:
    """Hand a facet response to the walk that covers the same navParam.

    A split node's children have their own navParams and get their own facet
    reads, so only an exact match is reusable.
    """
    if raw is None:
        return
    for walk in walks:
        if walk.nav_param == nav_param:
            walk.primed = raw


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:80]


def walk_status(walk: Walk) -> str:
    """complete | truncated | failed — how far this walk's coverage can be trusted.

    Judged on what was seen, not on how the walk ended: a walk that saw every
    itemId its node claimed is complete even if its final request errored,
    and a walk that ran to a clean stop but saw fewer than the node's own
    total is truncated — catalog churn mid-walk can cause that, and erring
    toward under-claiming is the safe direction. "failed" is reserved for a
    walk that produced nothing usable at all, not even a page-0 total.
    """
    observed = walk.observed_ids or 0
    if observed == 0 and walk.live_total is None:
        return "failed"
    denom = walk.live_total if walk.live_total is not None else walk.total
    if denom and observed >= denom and not walk.truncated:
        return "complete"
    if walk.truncated or walk.cut or (denom and observed < denom):
        return "truncated"
    return "complete"
