"""The scheduled scan as a service: watchlist, browse, probe, diff, alerts, notify.

`hd run-once` is a thin wrapper over `run_pipeline`. Keeping the orchestration
here means it can be driven from anywhere — a test, another command, a
container entry point — without going through the CLI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from hd.config import Settings


async def _report_scan_health(settings: Settings, *, ok: bool) -> str | None:
    """Fold this run into the liveness state and announce any transition.

    Health goes out on its own Slack message rather than through the deal
    notifier: deal alerts are grouped and formatted around a store and an item,
    and they spike — 68 in one day on 2026-08-17. A "scanning stopped" notice
    buried in that is a notice nobody sees.
    """
    from datetime import datetime, timezone

    from hd.logging import get_logger
    from hd.pipeline.health import (
        emit_health_transition_alert,
        load_scan_health,
        next_scan_health,
        outage_duration_hours,
        save_scan_health,
    )

    log = get_logger("pipeline.health")
    now = datetime.now(timezone.utc)
    before = load_scan_health(settings.health_state_path)
    after, transition = next_scan_health(before, ok, now)
    save_scan_health(settings.health_state_path, after)

    if transition is None:
        if after.status.value == "DEGRADED":
            log.warning(
                "Still degraded — already reported",
                consecutive_failures=after.consecutive_failures,
            )
        return None

    down_for = outage_duration_hours(before, now)
    await emit_health_transition_alert(settings, transition, after, down_for)

    if not settings.health_notify or not settings.slack_bot_token:
        return transition

    if transition == "degraded":
        text = (
            ":warning: *HD Scanner stopped collecting.* "
            "The last run captured no snapshots — usually Home Depot throttling. "
            "You will get one more message when it resumes."
        )
    else:
        span = f" after {down_for:.0f}h" if down_for else ""
        text = f":white_check_mark: *HD Scanner is collecting again*{span}."

    from hd.notifiers.webhook import post_to_slack

    if await post_to_slack(settings, text):
        log.info("Health transition sent to Slack", transition=transition)
    else:
        log.warning("Health transition could not be sent", transition=transition)
    return transition


async def run_pipeline(
    settings: Settings,
    mode: str,
    console,
    post_run_hooks: Callable[[], list],
) -> tuple[int, int, int, int, int, str]:
    """Run one scan end to end.

    Returns (products, snapshots, alerts, sent, canvas_deals, effective_mode).
    `effective_mode` is "cooldown" when the run was deferred without reading
    anything.
    """
    from pathlib import Path
    from datetime import datetime, timezone
    from hd.db.base import init_db as _init_tables, close_db
    from hd.pipeline.discovery import run_discovery
    from hd.pipeline.snapshot import run_snapshots
    from hd.pipeline.diff import run_diff
    from hd.pipeline.alerts import write_alerts
    from hd.http.client import HDClient
    from hd.logging import get_logger

    log = get_logger("pipeline")

    from hd.pipeline.digest import RunReport, previously_seen, write_digest
    from hd.pipeline.watchlist import request_allowance, run_watchlist

    report = RunReport(
        started=datetime.now(timezone.utc), stores=list(settings.store_list)
    )

    async def _digest() -> None:
        # The digest is a convenience on top of the scan and must never
        # fail it — but a digest that could not be written is said loudly.
        try:
            path = await write_digest(settings, report)
            if path:
                console.print(f"[dim]Digest: {path}[/dim]")
        except Exception as exc:
            log.error("Digest could not be written", error=str(exc))
            console.print(f"[red]Digest could not be written: {exc}[/red]")
        if not settings.site_worker:
            return
        from rich.markup import escape as _esc

        try:
            from hd.pipeline.site import publish_site

            ok, message = await publish_site(settings, report)
            style = "dim" if ok else "red"
            label = "Deals page" if ok else "Deals page could not be published"
            console.print(f"[{style}]{label}: {_esc(message)}[/{style}]")
        except Exception as exc:
            log.error("Deals page could not be published", error=str(exc))
            console.print(f"[red]Deals page could not be published: {_esc(str(exc))}[/red]")

    # Determine effective mode
    effective_mode = mode
    if mode == "auto":
        hour = datetime.now(timezone.utc).hour
        effective_mode = "full" if hour == 0 else "snapshot-only"

    # A 206 from an earlier run holds across process restarts. Starting a
    # scan inside that window just burns requests into a wall — the 20:00
    # run on 2026-08-19 was throttled on its second request doing exactly
    # this — so the run defers instead.
    from hd.http.cooldown import ThrottleCooldown

    cooldown = ThrottleCooldown(
        settings.throttle_cooldown_path, settings.throttle_cooldown_seconds
    )
    if cooldown.is_active():
        log.warning(
            "Deferring run — Home Depot throttled an earlier run",
            resumes_in_seconds=round(cooldown.remaining_seconds()),
        )
        report.mode = "deferred"
        report.has_alerts = False
        report.degraded.append(
            "Run deferred without reading anything: Home Depot throttled an "
            f"earlier run. Resumes in about {round(cooldown.remaining_seconds() / 60)} min."
        )
        await _digest()
        return 0, 0, 0, 0, 0, "cooldown"

    log.info("Pipeline starting", mode=effective_mode, browse=settings.browse_enabled)
    await _init_tables(settings)

    # The watchlist gets its own allowance on top of the scan's budget, so
    # hand-picked items never starve the brand walk or the reverse.
    watch_allowance = request_allowance(settings)
    if settings.browse_enabled:
        base_budget = settings.browse_request_budget
    else:
        base_budget = settings.request_budget
    shared_client = HDClient(
        settings,
        request_budget=(
            base_budget + watch_allowance + settings.probe_max_per_run
            if base_budget > 0 else 0
        ),
    )
    product_count = 0
    snapshot_count = 0
    browse_summary = None
    watch = None

    try:
        # Watchlist first: a small, fixed cost, and these are the items
        # nothing else in the run will look at.
        if watch_allowance:
            watch = await run_watchlist(
                settings, shared_client, settings.store_list, now=report.started
            )
            product_count += watch.products
            snapshot_count += watch.snapshots

        if settings.browse_enabled:
            from hd.pipeline.browse import (
                current_et_hour,
                parse_et_hours,
                run_browse,
            )

            # Per-hour tier assignment. With browse_shelf_hours_et set, each
            # run is dedicated to one tier: the IN_STORE shelf at its hours
            # (spread across them via the rotation fraction), the ALL/online
            # catalogue at every other scan hour. Empty keeps the original
            # both-tiers-every-run behaviour.
            # One instant for the whole run. The daily-deals sweep runs
            # between this line and the browse, and can carry a run that
            # started at 03:59 past the hour: the tier chosen here and the
            # browse's own hour-keyed decisions must read the same clock.
            run_started = datetime.now(timezone.utc)
            et_hour = current_et_hour(run_started)
            shelf_hours = parse_et_hours(settings.browse_shelf_hours_et)
            dd_hours = parse_et_hours(settings.daily_deals_hours_et)
            if shelf_hours:
                tiers = ("shelf",) if et_hour in shelf_hours else ("network",)
            else:
                tiers = ("shelf", "network")

            # Cold-tier gate: the costly online-network walk runs at most
            # once per interval. A run left with no tier still does the hot
            # work (watchlist, daily deals) and says so in its digest.
            from hd.pipeline.tiering import gate_tiers, mark_cold_completed

            tiers = gate_tiers(settings, tiers, run_started)

            # Daily Deals goes first, ahead of the browse tiers. A daily
            # deal is valid for one day; shelf clearance persists for days.
            # The brand gate means this usually spends no API requests at all.
            run_dd = settings.daily_deals_enabled and (
                not dd_hours or et_hour in dd_hours
            )
            if run_dd:
                from hd.pipeline.daily_deals import run_daily_deals

                dd = await run_daily_deals(settings)
                if not dd.skipped:
                    product_count += dd.products
                    snapshot_count += dd.snapshots
                    log.info(
                        "Daily deals swept",
                        end_date=dd.end_date,
                        brand_matches=dd.brand_matches,
                        snapshots=dd.snapshots,
                    )

            # Facet-driven browse replaces keyword discovery + snapshots:
            # both tiers upsert products and append snapshots per page.
            if tiers:
                summary = await run_browse(
                    settings=settings,
                    store_ids=settings.store_list,
                    client=shared_client,
                    tiers=tiers,
                    now=run_started,
                )
                browse_summary = summary
                product_count += summary.products
                snapshot_count += summary.snapshots
                if "network" in tiers and not (
                    summary.aborted
                    or summary.truncated_walks
                    or summary.deferred_walks
                    or summary.deferred_categories
                    or shared_client.is_throttled
                ):
                    mark_cold_completed(settings, run_started)
            else:
                report.notes.append(
                    "The online-network tier was not due this run "
                    f"(it runs about every {settings.network_tier_min_interval_hours:g} h); "
                    "only the watchlist and daily deals were read."
                )
            effective_mode = "browse"
            log.info("Tiers this run", hour_et=et_hour, tiers=list(tiers), daily_deals=run_dd)

        else:
            if effective_mode == "full":
                product_count = await run_discovery(
                    settings=settings,
                    brands=settings.brand_list,
                    max_pages=settings.max_pages,
                    client=shared_client,
                )
                log.info("Discovery complete", products=product_count, requests=shared_client.request_count)

                if settings.stage_delay_seconds > 0:
                    await asyncio.sleep(settings.stage_delay_seconds)

            snapshot_count += await run_snapshots(
                settings=settings,
                store_ids=settings.store_list,
                client=shared_client,
            )
        if settings.probe_max_per_run > 0 and not shared_client.is_throttled:
            from hd.pipeline.probe import run_probe

            probe = await run_probe(settings, shared_client, now=report.started)
            snapshot_count += probe.refreshed
            if probe.candidates:
                report.notes.append(
                    f"Re-read {probe.refreshed} of {len(probe.candidates)} top deal(s) "
                    "individually just before alerting."
                )
        log.info("Scan complete", products=product_count, rows=snapshot_count, requests=shared_client.request_count)
    except Exception as exc:
        # A crashed run still leaves a digest entry saying it saw nothing
        # reliable, then fails as it would have.
        report.mode = "failed"
        report.snapshots = snapshot_count
        report.requests = shared_client.request_count
        report.has_alerts = False
        report.degraded.append(f"The run stopped on an error before finishing: {exc}")
        await _digest()
        raise
    finally:
        metrics = shared_client.metrics
        log.info("Request metrics", **metrics.summary())
        metrics.append_jsonl(
            settings.metrics_path,
            ts=datetime.now(timezone.utc).isoformat(),
            mode=effective_mode,
            stores=len(settings.store_list),
        )
        # Escaped: the outcome breakdown is bracketed, which Rich would
        # otherwise parse as a style tag and silently drop — the 04:00 run
        # printed its summary with the failure reasons missing.
        from rich.markup import escape

        console.print(f"[dim]HTTP: {escape(metrics.render())}[/dim]")
        report.requests = shared_client.request_count
        if shared_client.is_throttled:
            fails = shared_client.failures
            if fails.get("soft_block"):
                why = ("Home Depot answered with implausibly empty pages (a suspected "
                       "soft block). Those pages were discarded, not recorded.")
            elif fails.get("http_403") or fails.get("challenge_html"):
                why = "Home Depot refused the requests (HTTP 403 or a challenge page)."
            else:
                why = "Home Depot throttled this run (HTTP 206)."
            report.degraded.append(f"{why} Everything after that point was not read.")
        await shared_client.close()

    report.mode = effective_mode
    report.snapshots = snapshot_count
    nothing_due = settings.browse_enabled and not tiers
    if snapshot_count == 0 and not nothing_due:
        report.degraded.append("The run captured no price readings at all.")
    if browse_summary is not None:
        if browse_summary.aborted and not report.degraded:
            report.degraded.append("The category walk stopped before it finished.")
        if browse_summary.truncated_walks:
            report.degraded.append(
                f"{len(browse_summary.truncated_walks)} category walk(s) were cut short: "
                + ", ".join(str(w) for w in browse_summary.truncated_walks[:10])
            )
        short = browse_summary.deferred_walks + browse_summary.deferred_categories
        if short:
            report.degraded.append(
                f"The request budget ran out: {browse_summary.deferred_walks} walk(s) and "
                f"{browse_summary.deferred_categories} categor(ies) were not read."
            )
        if browse_summary.skipped_walks:
            report.notes.append(
                f"{browse_summary.skipped_walks} category walk(s) were left for a later "
                "run by the normal rotation."
            )
    if watch is not None:
        if watch.failed:
            report.degraded.append(
                f"{len(watch.failed)} watchlist lookup(s) failed or were never sent: "
                + ", ".join(f"{i} at #{s}" for s, i in watch.failed[:10])
            )
        if watch.capped:
            report.degraded.append(
                f"The watchlist holds {watch.listed} items; the last {watch.capped} "
                "were not checked (WATCHLIST_MAX_ITEMS)."
            )
        if watch.not_returned:
            # An item that used to answer and now does not is suspicious;
            # one that never has is more likely a wrong id.
            known = await previously_seen(settings, watch.not_returned)
            lost = [pair for pair in watch.not_returned if pair in known]
            fresh = [pair for pair in watch.not_returned if pair not in known]
            if lost:
                report.degraded.append(
                    "Watchlist items seen before came back empty: "
                    + ", ".join(f"{i} at #{s}" for s, i in lost[:10])
                )
            if fresh:
                report.notes.append(
                    "Watchlist items the API has never returned (check the ids): "
                    + ", ".join(f"{i} at #{s}" for s, i in fresh[:10])
                )
        if watch.invalid_lines:
            report.notes.append(
                "Watchlist lines ignored because they are not item ids: "
                + ", ".join(watch.invalid_lines[:10])
            )

    # Liveness: a run that captured nothing is a blind run. Report the
    # transition, not the state — see hd.pipeline.health.
    if not (nothing_due and snapshot_count == 0):
        await _report_scan_health(settings, ok=snapshot_count > 0)

    alerts_list = await run_diff(settings=settings)
    alert_count = 0
    if alerts_list:
        alert_count = await write_alerts(settings=settings, alerts=alerts_list)
    log.info("Diff complete", alerts=alert_count)

    await _digest()
    if report.degraded:
        console.print("[yellow]HEALTH_DEGRADED — this run did not see everything:[/yellow]")
        for reason in report.degraded:
            console.print(f"[yellow]  - {reason}[/yellow]")

    # Auto-notify Slack when new alerts are found
    sent_count = 0
    if alert_count > 0 and settings.slack_bot_token:
        from hd.dashboard.queries import get_alerts
        from hd.grouping import group_alerts, parse_ts
        from hd.notifiers.formatter import format_slack_blocks
        from hd.notifiers.webhook import post_to_slack

        cursor_path = Path(settings.notify_cursor_path)
        cursor_ts = None
        if cursor_path.exists():
            try:
                cursor_ts = datetime.fromisoformat(cursor_path.read_text().strip())
            except (ValueError, OSError):
                pass

        hours_back = max(4, 168) if cursor_ts else 4
        recent = await get_alerts(settings, since_hours=hours_back, limit=500)
        if cursor_ts is not None:
            recent = [a for a in recent if parse_ts(a.get("ts")) > cursor_ts]
        recent = [
            a for a in recent
            if a.get("alert_type") not in ("HEALTH_DEGRADED", "HEALTH_RECOVERED")
        ]

        if recent:
            groups = group_alerts(recent)
            blocks, fallback_text = format_slack_blocks(groups)
            success = await post_to_slack(settings, fallback_text, blocks=blocks)
            if success:
                max_ts = max(
                    (parse_ts(a.get("ts")) for a in recent), default=None
                )
                if max_ts is not None:
                    cursor_path.write_text(max_ts.isoformat())
                sent_count = len(groups)
                log.info("Slack notification sent", groups=sent_count)
            else:
                log.warning("Slack notification failed")

    # Update Slack canvas with current deal rundown
    canvas_deals = 0
    if settings.slack_bot_token and snapshot_count > 0:
        try:
            from hd.notifiers.canvas import run_canvas_update
            _, canvas_deals = await run_canvas_update(settings)
            log.info("Canvas updated", deals=canvas_deals)
        except Exception as e:
            log.warning("Canvas update failed", error=str(e))

    # Optional post-run plugins. A failure is logged and deliberately
    # never touches scan results: the scan is the product, a plugin is not.
    for _name, _hook in post_run_hooks():
        try:
            _result = await _hook(settings)
            log.info("Post-run plugin", plugin=_name, **(_result or {}))
        except Exception as exc:
            log.warning("Post-run plugin failed", plugin=_name, error=str(exc))

    await close_db()
    return product_count, snapshot_count, alert_count, sent_count, canvas_deals, effective_mode

