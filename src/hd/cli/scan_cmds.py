"""Scan commands: discover, snapshot, browse, daily deals, catch-up."""

from __future__ import annotations

import typer
from hd.config import Settings
from hd.db.models import AlertType
from hd.logging import setup_logging
from rich.table import Table
from typing import Optional
from hd.cli._common import _need_brand_tokens, _need_brands, _need_stores, _run, app, console


@app.command()
def discover(
    brand: Optional[list[str]] = typer.Option(None, help="Brand(s) to discover"),
    pages: int = typer.Option(0, help="Max pages per brand (0 = use config)"),
    clearance_only: bool = typer.Option(False, "--clearance-only", help="Only discover clearance items"),
) -> None:
    """Run product discovery pipeline."""
    setup_logging()
    settings = Settings()
    brands = _need_brands(brand if brand else settings.brand_list)

    async def _discover():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.discovery import run_discovery

        await _init_tables(settings)
        max_pages = pages if pages > 0 else settings.max_pages
        count = await run_discovery(
            settings=settings,
            brands=brands,
            max_pages=max_pages,
            clearance_only=clearance_only,
        )
        await close_db()
        return count

    count = _run(_discover())
    console.print(f"[green]Discovery complete: {count} products found/updated.[/green]")


@app.command()
def snapshot(
    stores: Optional[str] = typer.Option(None, help="Comma-separated store IDs"),
    limit: int = typer.Option(0, help="Max products to snapshot (0 = all)"),
) -> None:
    """Fetch pricing/inventory snapshots for active products."""
    setup_logging()
    settings = Settings()
    store_ids = _need_stores(
        [s.strip() for s in stores.split(",") if s.strip()] if stores else settings.store_list
    )

    async def _snapshot():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.snapshot import run_snapshots

        await _init_tables(settings)
        count = await run_snapshots(
            settings=settings,
            store_ids=store_ids,
            limit=limit if limit > 0 else None,
        )
        await close_db()
        return count

    count = _run(_snapshot())
    console.print(f"[green]Snapshots complete: {count} rows inserted.[/green]")


@app.command()
def browse(
    stores: Optional[str] = typer.Option(None, help="Comma-separated store IDs (default: config)"),
    tier: str = typer.Option("both", help="Tier to run: shelf, network, or both"),
) -> None:
    """Facet-driven brand browse: discover + snapshot every brand item by category."""
    setup_logging()
    settings = Settings()
    store_ids = _need_stores(
        [s.strip() for s in stores.split(",") if s.strip()] if stores else settings.store_list
    )
    _need_brand_tokens(settings)

    async def _browse():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.browse import run_browse

        await _init_tables(settings)
        tiers = ("shelf", "network") if tier == "both" else (tier,)
        summary = await run_browse(settings=settings, store_ids=store_ids, tiers=tiers)
        await close_db()
        return summary

    summary = _run(_browse())
    msg = (
        f"[green]Browse complete: {summary.products} products, "
        f"{summary.snapshots} snapshots across {summary.walks} walk(s)."
    )
    if summary.truncated_walks:
        msg += f" [yellow]Truncated: {', '.join(summary.truncated_walks)}[/yellow]"
    if summary.aborted:
        msg += " [red](aborted early — throttled)[/red]"
    msg += "[/green]"
    console.print(msg)


@app.command()
def daily_deals(
    force: bool = typer.Option(False, "--force", help="Re-process even if today's set was already swept"),
    wait_for_refresh: bool = typer.Option(
        False, "--wait-for-refresh",
        help="Re-read the page every DAILY_DEALS_POLL_SECONDS until the set changes "
             "(at most DAILY_DEALS_POLL_MAX reads), then sweep. Start it at 3:00 Eastern.",
    ),
    alert: bool = typer.Option(
        True, "--alert/--no-alert",
        help="After a sweep, diff the new snapshots and write alerts; `hd notify` delivers them. "
             "--no-alert loses the drop alert for good: the next run's diff compares this "
             "sweep's price with its own and sees no change.",
    ),
) -> None:
    """Price today's Daily Deals set (Special Buy of the Day) for configured brands."""
    if force and wait_for_refresh:
        console.print("[red]--force re-sweeps the set on the page now; --wait-for-refresh waits "
                      "for it to change. Pick one.[/red]")
        raise typer.Exit(code=2)
    setup_logging()
    settings = Settings()
    _need_stores(settings.store_list)
    _need_brand_tokens(settings)

    async def _daily():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.logging import get_logger
        from hd.pipeline.daily_deals import run_daily_deals, wait_for_refresh as _wait

        log = get_logger("pipeline.daily_deals")
        await _init_tables(settings)
        alert_count = 0
        try:
            if wait_for_refresh:
                summary = await _wait(settings)
            else:
                summary = await run_daily_deals(settings, force=force)
            if alert and not summary.skipped and summary.snapshots:
                from hd.pipeline.alerts import write_alerts
                from hd.pipeline.diff import run_diff

                found = await run_diff(settings=settings)
                if found:
                    alert_count = await write_alerts(settings=settings, alerts=found)
                log.info("Diff complete", alerts=alert_count)
        finally:
            await close_db()
        return summary, alert_count

    s, alert_count = _run(_daily())
    if s.stopped == "unavailable":
        console.print(
            f"[red]Daily-deals page unavailable on read {s.polls} — stopped without retry. "
            "The next scheduled run sweeps the set as usual.[/red]"
        )
        raise typer.Exit(code=2)
    if s.stopped in ("unchanged", "late"):
        why = "started too far from 3:00 Eastern to poll" if s.stopped == "late" else "unchanged"
        console.print(
            f"[yellow]Daily-deals set {why} after {s.polls} read(s) — "
            "left to the next scheduled run.[/yellow]"
        )
    elif s.stopped == "cooldown":
        console.print("[yellow]Deferred — an earlier run was throttled and the cooldown is still active. "
                      "No page was read.[/yellow]")
    elif s.stopped == "locked":
        console.print("[yellow]Another sweep is running — nothing done here.[/yellow]")
    elif s.skipped:
        console.print("[yellow]Skipped — set already processed or page unavailable "
                      "(use --force to re-run).[/yellow]")
    else:
        when = f" {s.seconds_to_flip:.0f}s after the poll began" if s.seconds_to_flip is not None else ""
        console.print(
            f"[green]Daily deals ({s.end_date}){when}: {s.items_checked} items checked, "
            f"{s.brand_matches} brand match(es), {s.snapshots} snapshot(s), "
            f"{alert_count} alert(s).[/green]"
        )


@app.command()
def catch_up(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be created without writing"),
) -> None:
    """One-time scan: alert on anything currently ≥50% off or in Special Buys that has never been alerted."""
    setup_logging()
    settings = Settings()
    _need_stores(settings.store_list)

    async def _catch_up():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.pipeline.diff import run_catch_up
        from hd.pipeline.alerts import write_alerts

        await _init_tables(settings)
        alerts_list = await run_catch_up(settings=settings)

        if dry_run:
            await close_db()
            return alerts_list, 0

        written = 0
        if alerts_list:
            written = await write_alerts(settings=settings, alerts=alerts_list)
        await close_db()
        return alerts_list, written

    alerts_list, written = _run(_catch_up())

    if not alerts_list:
        console.print("[yellow]No catch-up alerts needed — everything is already covered.[/yellow]")
        return

    if dry_run:
        table = Table(title="Catch-up Alerts (dry run)")
        table.add_column("Store", style="green")
        table.add_column("Item", style="white")
        table.add_column("Type", style="magenta")
        table.add_column("Severity", style="red")
        table.add_column("Details", style="dim")

        for a in alerts_list:
            payload = a.payload or {}
            after = payload.get("after", {})
            if a.alert_type == AlertType.DEEP_DISCOUNT:
                details = f"{payload.get('percentage_off', '?')}% off @ ${after.get('price_value', '?')}"
            elif a.alert_type == AlertType.SPECIAL_BUY:
                details = f"Special Buy @ ${after.get('price_value', '?')}"
            elif a.alert_type == AlertType.IN_STORE_CLEARANCE:
                cl = payload.get("clearance_value", "?")
                pct = payload.get("clearance_percentage_off", "?")
                details = f"In-store ${cl} ({pct}% off)"
            else:
                details = payload.get("product_title", "")[:40]
            table.add_row(a.store_id, a.item_id, a.alert_type.value, a.severity.value, details)

        console.print(table)
        console.print(f"\n[cyan]{len(alerts_list)} alert(s) would be created. Run without --dry-run to persist.[/cyan]")
    else:
        console.print(f"[green]Catch-up complete: {written} alerts created.[/green]")
