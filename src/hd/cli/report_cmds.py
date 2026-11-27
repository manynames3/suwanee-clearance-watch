"""Commands that report: alerts, doctor, health, notify, canvas, dashboard."""

from __future__ import annotations

import typer
from hd.config import Settings
from hd.logging import setup_logging
from rich.table import Table
from typing import Optional
from hd.cli._common import _run, app, console


@app.command()
def alerts(
    limit: int = typer.Option(20, help="Number of alerts to show"),
    type_filter: Optional[str] = typer.Option(None, "--type", help="Filter by alert type"),
    since: Optional[int] = typer.Option(None, help="Show alerts from last N hours"),
) -> None:
    """Print recent alerts."""
    setup_logging()
    settings = Settings()

    async def _alerts():
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, desc
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import Alert, AlertType

        await _init_tables(settings)

        async with get_session(settings) as session:
            stmt = select(Alert)

            if type_filter:
                try:
                    at = AlertType(type_filter)
                    stmt = stmt.where(Alert.alert_type == at)
                except ValueError:
                    console.print(f"[red]Unknown alert type: {type_filter}[/red]")
                    return []

            if since:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=since)
                stmt = stmt.where(Alert.ts >= cutoff)

            stmt = stmt.order_by(desc(Alert.ts)).limit(limit)

            result = await session.execute(stmt)
            rows = result.scalars().all()

        await close_db()
        return rows

    rows = _run(_alerts())

    if not rows:
        console.print("[yellow]No alerts found.[/yellow]")
        return

    table = Table(title="Recent Alerts")
    table.add_column("Time", style="cyan")
    table.add_column("Store", style="green")
    table.add_column("Item", style="white")
    table.add_column("Type", style="magenta")
    table.add_column("Severity", style="red")
    table.add_column("Details", style="dim")

    for row in rows:
        payload = row.payload or {}
        details = ""
        if row.alert_type.value == "PRICE_DROP":
            before = payload.get("before", {}).get("price_value", "?")
            after = payload.get("after", {}).get("price_value", "?")
            details = f"${before} -> ${after}"
        elif row.alert_type.value == "CLEARANCE":
            pct = payload.get("after", {}).get("percentage_off", "?")
            details = f"{pct}% off"
        elif row.alert_type.value == "IN_STORE_CLEARANCE":
            cl = payload.get("clearance_value", "?")
            pct = payload.get("clearance_percentage_off", "?")
            details = f"${cl} ({pct}% off) in-store"
        else:
            title = payload.get("product_title", "")
            details = title[:40] if title else ""

        table.add_row(
            str(row.ts)[:19],
            row.store_id,
            row.item_id,
            row.alert_type.value,
            row.severity.value,
            details,
        )

    console.print(table)


@app.command()
def doctor(
    fix: bool = typer.Option(
        False, "--fix", help="Apply the repairs that are safe to automate"
    ),
    network: bool = typer.Option(
        False, "--network",
        help="Diagnose request trouble (quota, refusal, soft block, connection) from recorded runs",
    ),
) -> None:
    """Check that this installation is wired up correctly."""
    setup_logging()
    settings = Settings()

    if network:
        from hd.doctor import FAIL, OK, WARN
        from hd.network_doctor import check_network

        marks = {OK: "[green]OK  [/green]", WARN: "[yellow]WARN[/yellow]", FAIL: "[red]FAIL[/red]"}
        table = Table(title="HD Scanner — network check")
        table.add_column("", width=4)
        table.add_column("check", style="cyan")
        table.add_column("detail")
        results = check_network(settings)
        for c in results:
            table.add_row(marks.get(c.status, c.status), c.name, c.detail)
        console.print(table)
        for c in results:
            if c.fix:
                console.print(f"[bold]{c.name}:[/bold] {c.fix}")
        raise typer.Exit(1 if any(c.failed for c in results) else 0)

    async def _doctor():
        from hd.db.base import close_db
        from hd.doctor import run_checks

        checks = await run_checks(settings)
        await close_db()
        return checks

    checks = _run(_doctor())

    from hd.doctor import FAIL, OK, WARN

    marks = {OK: "[green]OK  [/green]", WARN: "[yellow]WARN[/yellow]", FAIL: "[red]FAIL[/red]"}
    table = Table(title="HD Scanner — installation check")
    table.add_column("", width=4)
    table.add_column("check", style="cyan")
    table.add_column("detail")
    for c in checks:
        table.add_row(marks.get(c.status, c.status), c.name, c.detail)
    console.print(table)

    if fix:
        from hd.doctor import apply_fixes

        applied = _run(apply_fixes(settings, checks))
        if applied:
            console.print("\n[bold]Repairs[/bold]")
            for line in applied:
                console.print(f"  {line}")
            console.print("\n[dim]Re-run `hd doctor` to confirm.[/dim]")
        else:
            console.print("\n[dim]Nothing to repair automatically.[/dim]")
        return

    fixes = [c for c in checks if c.fix]
    if fixes:
        console.print("\n[bold]Suggested fixes[/bold]")
        for c in fixes:
            console.print(f"  [dim]{c.name}:[/dim] {c.fix}")
        console.print("\n[dim]`hd doctor --fix` applies the automatable ones.[/dim]")

    failed = sum(1 for c in checks if c.status == FAIL)
    warned = sum(1 for c in checks if c.status == WARN)
    if failed:
        console.print(f"\n[red]{failed} failing, {warned} warning[/red]")
        raise typer.Exit(code=1)
    console.print(f"\n[green]All clear[/green]" if not warned else f"\n[yellow]{warned} warning(s)[/yellow]")


@app.command()
def health() -> None:
    """Print last run health status."""
    setup_logging()
    settings = Settings()

    async def _health():
        from sqlalchemy import select, desc, func
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import Alert, AlertType, StoreSnapshot, Product

        await _init_tables(settings)

        async with get_session(settings) as session:
            # Check for recent HEALTH_DEGRADED alerts
            result = await session.execute(
                select(Alert)
                .where(Alert.alert_type == AlertType.HEALTH_DEGRADED)
                .order_by(desc(Alert.ts))
                .limit(1)
            )
            degraded_alert = result.scalar_one_or_none()

            # Get counts
            product_count = (await session.execute(
                select(func.count()).select_from(Product).where(Product.is_active.is_(True))
            )).scalar() or 0

            snapshot_count = (await session.execute(
                select(func.count()).select_from(StoreSnapshot)
            )).scalar() or 0

            latest_snapshot = (await session.execute(
                select(StoreSnapshot.ts).order_by(desc(StoreSnapshot.ts)).limit(1)
            )).scalar_one_or_none()

        await close_db()
        return degraded_alert, product_count, snapshot_count, latest_snapshot

    degraded, products, snapshots, latest_ts = _run(_health())

    status = "[red]DEGRADED[/red]" if degraded else "[green]HEALTHY[/green]"
    console.print(f"Status: {status}")
    console.print(f"Active products: {products}")
    console.print(f"Total snapshots: {snapshots}")
    if latest_ts:
        console.print(f"Latest snapshot: {str(latest_ts)[:19]}")
    if degraded:
        console.print(f"[red]Last degraded alert: {str(degraded.ts)[:19]}[/red]")
        payload = degraded.payload or {}
        if "message" in payload:
            console.print(f"[red]  {payload['message']}[/red]")


@app.command()
def notify(
    since: int = typer.Option(4, help="Fallback hours if no cursor exists"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print without sending"),
    reset: bool = typer.Option(False, "--reset", help="Clear cursor and re-send"),
) -> None:
    """Send recent alerts to Slack."""
    from pathlib import Path

    setup_logging()
    settings = Settings()

    cursor_path = Path(settings.notify_cursor_path)

    # Handle --reset
    if reset and cursor_path.exists():
        cursor_path.unlink()
        console.print("[yellow]Cursor reset.[/yellow]")

    # Read cursor timestamp
    cursor_ts = None
    if cursor_path.exists():
        try:
            from datetime import datetime

            raw = cursor_path.read_text().strip()
            cursor_ts = datetime.fromisoformat(raw)
        except (ValueError, OSError):
            console.print("[yellow]Invalid cursor file, using --since fallback.[/yellow]")

    async def _notify():
        from datetime import datetime, timedelta, timezone

        from hd.db.base import init_db as _init_tables, close_db
        from hd.db.repository import get_alerts
        from hd.grouping import group_alerts
        from hd.notifiers.formatter import format_slack_message, format_slack_blocks
        from hd.notifiers.webhook import post_to_slack

        await _init_tables(settings)

        # Determine how far back to query
        if cursor_ts is not None:
            # Query with a generous window; we filter post-query
            hours_back = max(since, 168)  # up to 7 days
        else:
            hours_back = since

        alerts_list = await get_alerts(settings, since_hours=hours_back, limit=500)

        # Filter to alerts after cursor_ts
        from hd.grouping import parse_ts

        if cursor_ts is not None:
            alerts_list = [
                a for a in alerts_list
                if parse_ts(a.get("ts")) > cursor_ts
            ]

        # Liveness alerts are delivered as their own Slack message the moment
        # the transition happens (see _report_scan_health); routing them through
        # the deal grouper as well would both duplicate them and format a
        # SYSTEM row as if it were a product.
        alerts_list = [
            a for a in alerts_list
            if a.get("alert_type") not in ("HEALTH_DEGRADED", "HEALTH_RECOVERED")
        ]

        if not alerts_list:
            await close_db()
            return 0, None, None

        groups = group_alerts(alerts_list)
        blocks, fallback_text = format_slack_blocks(groups)
        plain_message = format_slack_message(groups)

        # Find max timestamp for cursor update
        max_ts = max(
            (parse_ts(a.get("ts")) for a in alerts_list),
            default=None,
        )

        if dry_run:
            console.print(plain_message)
            await close_db()
            return len(groups), max_ts, True

        # Validate Slack token is configured
        if not settings.slack_bot_token:
            console.print("[red]SLACK_BOT_TOKEN not set. Use --dry-run to preview.[/red]")
            await close_db()
            return len(groups), max_ts, False

        success = await post_to_slack(settings, fallback_text, blocks=blocks)
        await close_db()
        return len(groups), max_ts, success

    group_count, max_ts, success = _run(_notify())

    if group_count == 0:
        console.print("[yellow]No new alerts to send.[/yellow]")
        return

    if dry_run:
        console.print(f"\n[cyan]--- Dry run: {group_count} alert group(s) above ---[/cyan]")
        return

    if success:
        # Update cursor
        if max_ts is not None:
            cursor_path.write_text(max_ts.isoformat())
        console.print(f"[green]Sent {group_count} alert group(s) to Slack.[/green]")
    else:
        console.print("[red]Webhook delivery failed. Cursor not updated.[/red]")


@app.command()
def canvas_update(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print markdown without sending to Slack"),
    reset: bool = typer.Option(False, "--reset", help="Delete canvas ID file and create a new canvas"),
) -> None:
    """Update the Slack canvas with current deal rundown per store."""
    from pathlib import Path

    setup_logging()
    settings = Settings()

    if reset:
        if not settings.canvas_enabled:
            # Deleting the id here would orphan the existing canvas: re-enabling
            # later creates a duplicate instead of resuming it.
            console.print(
                "[yellow]Canvas is disabled (CANVAS_ENABLED=false); leaving the "
                "stored canvas id alone.[/yellow]"
            )
            raise typer.Exit(0)
        canvas_path = Path(settings.canvas_id_path)
        if canvas_path.exists():
            canvas_path.unlink()
            console.print("[yellow]Canvas ID reset — will create a new canvas.[/yellow]")

    async def _canvas_update():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.notifiers.canvas import run_canvas_update

        await _init_tables(settings)
        markdown, deal_count = await run_canvas_update(settings, dry_run=dry_run)
        await close_db()
        return markdown, deal_count

    markdown, deal_count = _run(_canvas_update())

    if dry_run:
        console.print(markdown)
        console.print(f"\n[cyan]--- Dry run: {deal_count} deal(s) across all stores ---[/cyan]")
    else:
        console.print(f"[green]Canvas updated with {deal_count} deal(s).[/green]")


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Bind host (overrides config)"),
    port: Optional[int] = typer.Option(None, help="Bind port (overrides config)"),
    dark: bool = typer.Option(True, help="Dark mode"),
) -> None:
    """Start the NiceGUI web dashboard."""
    setup_logging()
    settings = Settings()

    if host:
        settings.dashboard_host = host
    if port:
        settings.dashboard_port = port
    settings.dashboard_dark_mode = dark

    try:
        from hd.dashboard.app import run_dashboard
    except ImportError:
        console.print("[red]NiceGUI not installed. Run: pip install -e '.[dashboard]'[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Starting dashboard at http://{settings.dashboard_host}:{settings.dashboard_port}[/green]"
    )
    run_dashboard(settings)  # Blocking — owns the event loop


@app.command("notify-test")
def notify_test() -> None:
    """Send one test message through every configured push channel."""
    setup_logging()
    settings = Settings()

    async def _test():
        from hd.notifiers.base import Notification
        from hd.notifiers.channels import ALL_NOTIFIERS

        sample = Notification(
            key="test", alert_type="TEST", store_id="0", store_name="",
            item_id="0", title="Test notification from Clearance Watch",
            deal_price=None, was_price=None, percent_off=None, confidence=None,
            confidence_label="", qty=None, url=None,
        )
        active = [n for n in ALL_NOTIFIERS if n.enabled(settings)]
        if not active:
            console.print("[yellow]No push channel is configured. See .env.example.[/yellow]")
            return 1
        failed = 0
        for n in active:
            ok = await n.send(settings, [sample])
            console.print(f"{'[green]ok[/green]' if ok else '[red]failed[/red]'}  {n.name}")
            failed += 0 if ok else 1
        return 1 if failed else 0

    raise typer.Exit(_run(_test()))
