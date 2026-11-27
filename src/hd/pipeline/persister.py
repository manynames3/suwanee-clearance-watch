"""Recording what a browse run did: per-walk coverage, run start/end, and the
coverage memory that later runs plan from."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hd.config import Settings
from hd.pipeline.nav_planner import BrowseSummary, Walk, walk_status

from hd.logging import get_logger

log = get_logger("pipeline.browse")


async def _record_walk(
    settings: Settings,
    run_id: int | None,
    walk: Walk,
    store_id: str,
    storefilter: str,
    started: datetime,
) -> None:
    """Persist one walk's coverage row. Never allowed to break a scan."""
    if run_id is None:
        return
    from hd.db.base import get_session
    from hd.db.models import WalkCoverage

    denom = walk.live_total if walk.live_total is not None else (walk.total or None)
    try:
        async with get_session(settings) as session:
            session.add(WalkCoverage(
                run_id=run_id,
                store_id=store_id,
                tier=storefilter,
                label=walk.label,
                nav_param=walk.nav_param,
                started=started,
                ended=datetime.now(timezone.utc),
                status=walk_status(walk),
                items_expected=denom,
                items_observed=walk.observed_ids or 0,
                split_parent=walk.split_parent,
                split_axis=walk.split_axis,
            ))
    except Exception as e:
        log.error("Coverage record failed", label=walk.label, error=str(e))


async def _record_run_start(settings: Settings, tiers: tuple[str, ...]) -> int | None:
    """Open this run's scan_runs row. Returns its id, or None if it could not
    be written — walks then go unrecorded, which downstream reads as
    unknown-not-complete: the safe failure mode."""
    from hd.db.base import get_session
    from hd.db.models import ScanRun

    try:
        async with get_session(settings) as session:
            run = ScanRun(
                started=datetime.now(timezone.utc),
                tiers=",".join(tiers),
                status="running",
            )
            session.add(run)
            await session.flush()
            return run.id
    except Exception as e:
        log.error("Could not open scan_runs row — walks will be unrecorded", error=str(e))
        return None


async def _record_run_end(
    settings: Settings, run_id: int | None, summary: BrowseSummary, requests_used: int
) -> None:
    if run_id is None:
        return
    from sqlalchemy import update

    from hd.db.base import get_session
    from hd.db.models import ScanRun

    try:
        async with get_session(settings) as session:
            await session.execute(
                update(ScanRun).where(ScanRun.id == run_id).values(
                    ended=datetime.now(timezone.utc),
                    status="aborted" if summary.aborted else "complete",
                    walks=summary.walks,
                    snapshots=summary.snapshots,
                    requests_used=requests_used,
                    # Deferred walks write no coverage row by design, so this
                    # is the only durable trace they leave. Written even when
                    # zero: "this run deferred nothing" is a real observation,
                    # and only a row predating the column reads NULL.
                    deferred_walks=summary.deferred_walks,
                    deferred_categories=summary.deferred_categories,
                )
            )
    except Exception as e:
        log.error("Could not finalize scan_runs row", run_id=run_id, error=str(e))


async def coverage_memory(
    settings: Settings, store_id: str, storefilter: str, refresh_hours: int,
) -> tuple[dict[str, int], set[str], dict[str, str], dict[str, list[tuple[str, str, int]]]]:
    """What the durable coverage record already knows about this tier's nodes.

    Returns (observed_totals, recently_completed, recent_axis, recorded_children).

    - **recorded_children**: for each node in `recent_axis`, the children its
      recorded split produced — (navParam, label, expected) per child, in
      every status, so a truncated band is rebuilt as the truncated walk it
      was rather than dropped. plan_walks uses this when the run's facets
      cannot build the recorded axis; see its docstring for the measured
      cost of not having it.

    - **observed_totals**: the largest page-0 total ever recorded per node, so a
      parent that under-reports a child cannot route it into a walk that cannot
      finish (see plan_walks).
    - **recently_completed**: nodes walked to `complete` inside the refresh
      window. A category too big for one run defers partway, and because
      `completed` only advances on a finished category the cursor stays put —
      so the next run RE-RESOLVES the same category and walks the same prefix
      again. Measured: MILWAUKEE/Tools was walked 157 times while every other
      Milwaukee category was walked once, and three consecutive runs opened
      with the identical six walks. Skipping what was just completed turns that
      restart into forward progress.

      Only nodes a walk actually carried. Rolling a completed split UP to the
      node it split was tried and withdrawn: see the note above plan_walks.

    - **recent_axis**: for each node that was split inside the refresh window,
      the axis its most recent recorded split used. Read straight off the
      `split_parent`/`split_axis` pair the planner wrote — a fact, not a shape
      inferred from the child navParams. plan_walks splits that node the same
      way again, so its children land on the navParams `recently_completed`
      already holds instead of arriving as an unrecognised second vocabulary.
      A node absent here is one the record has nothing recent to say about,
      and it is planned exactly as before.

      This carries no claim that the parent is covered. It decides which
      question to ask, never what the answer is; the answer still comes from
      `recently_completed`, one navParam at a time.

    Keyed on nav_param, never label: `label` falls back to the raw facet token
    when HD omits a label, so the same node can carry two labels across runs.
    Read-only, one query per tier per run, no API cost.
    """
    from sqlalchemy import func, select

    from hd.db import base
    from hd.db.models import WalkCoverage

    totals: dict[str, int] = {}
    recent: set[str] = set()
    axis_seen: dict[str, tuple[datetime, str]] = {}
    try:
        async with base.get_session(settings) as session:
            rows = (await session.execute(
                select(WalkCoverage.nav_param,
                       func.max(WalkCoverage.items_expected),
                       func.max(WalkCoverage.ended).filter(
                           WalkCoverage.status == "complete"))
                .where(WalkCoverage.nav_param.is_not(None),
                       WalkCoverage.store_id == store_id,
                       WalkCoverage.tier == storefilter)
                .group_by(WalkCoverage.nav_param))).all()
            axis_rows = (await session.execute(
                select(WalkCoverage.split_parent,
                       WalkCoverage.split_axis,
                       WalkCoverage.nav_param,
                       # Ranking is over splits that produced coverage. A
                       # truncated or failed child never enters `recent`, so
                       # it could never keep a preference alive — but ranked
                       # on its `ended` it could KILL one: a lone truncated
                       # re-walk of one band outranked a clean sweep on the
                       # other axis (run 65's single $400-$500 walk set the
                       # recorded axis at runs 68 and 69). Complete rows only.
                       func.max(WalkCoverage.ended).filter(
                           WalkCoverage.status == "complete"),
                       func.max(WalkCoverage.label),
                       func.max(WalkCoverage.items_expected))
                .where(WalkCoverage.split_parent.is_not(None),
                       WalkCoverage.split_axis.is_not(None),
                       WalkCoverage.store_id == store_id,
                       WalkCoverage.tier == storefilter)
                .group_by(WalkCoverage.split_parent,
                          WalkCoverage.split_axis,
                          WalkCoverage.nav_param))).all()
    except Exception as e:  # coverage memory is an optimisation, never a gate
        log.warning("Coverage memory unavailable — planning from facets only",
                    error=str(e))
        return {}, set(), {}, {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, refresh_hours))

    def _aware(ts: datetime | None) -> datetime | None:
        if ts is not None and ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    for nav, max_expected, last_complete in rows:
        if max_expected:
            totals[nav] = int(max_expected)
        last_complete = _aware(last_complete)
        if last_complete is not None and last_complete >= cutoff:
            recent.add(nav)

    # A parent that has been split on BOTH axes has rows for each; the more
    # recent split wins. Ties are left to the ordinary category-first default
    # rather than broken arbitrarily — a coin flip here is the flapping this
    # exists to stop.
    #
    # AND ONLY WHILE THAT SPLIT STILL HAS SOMETHING TO OFFER. The preference
    # is emitted for a parent only if at least one child of the recorded split
    # is itself still complete-in-window. Once every child has gone stale the
    # node is due for a full sweep, the freshness skip would skip nothing
    # whichever axis were chosen, and the preference is dropped so the ordinary
    # category-first order picks again.
    #
    # Without that clause the preference never lapses in practice and the node
    # is pinned to whichever axis it happened to start on: runs land every ~4h
    # against a 20h window, so "the last split is inside the window" is true
    # forever. Pinning was measured, in a replay of the real run history, to be
    # what the naive version actually does. Lapsing at the sweep boundary keeps
    # both axes exercised while still removing the mid-window flip, which is
    # the whole cost.
    # No cutoff test on the split time itself: it would be dead. A parent is
    # only emitted below if one of its children is in `recent`, and a child in
    # `recent` completed inside the window, so that child's own row already
    # puts the split inside it. Mutation testing found the redundant test —
    # removing it changed no outcome — so it is gone rather than left to look
    # like a guard that is holding something.
    for parent, ax, child_nav, last_split, _, _ in axis_rows:
        last_split = _aware(last_split)
        if last_split is None:
            continue
        prev = axis_seen.get(parent)
        if prev is None or last_split > prev[0]:
            axis_seen[parent] = (last_split, ax)
        elif last_split == prev[0] and ax != prev[1]:
            axis_seen[parent] = (last_split, "")

    # Every child of every recorded split, complete or not: the rebuild in
    # plan_walks must reproduce the split as it was, and a truncated band
    # left out of it would be a walk silently dropped.
    kids_by_split: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    for parent, ax, child_nav, _, child_label, expected in axis_rows:
        kids_by_split.setdefault((parent, ax), []).append(
            (child_nav, child_label or child_nav, int(expected or 0))
        )

    recent_axis: dict[str, str] = {}
    recorded_children: dict[str, list[tuple[str, str, int]]] = {}
    for parent, (_, ax) in axis_seen.items():
        if not ax:
            continue
        kids = kids_by_split.get((parent, ax), [])
        if any(nav in recent for nav, _, _ in kids):
            recent_axis[parent] = ax
            recorded_children[parent] = sorted(kids)
    return totals, recent, recent_axis, recorded_children
