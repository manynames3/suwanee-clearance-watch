"""Housekeeping commands: prune, backup, backfill."""

from __future__ import annotations

import typer
from hd.config import Settings
from hd.logging import setup_logging
from hd.cli._common import _run, app, console


def prune_raw_responses(settings: Settings, *, dry_run: bool = False) -> tuple[int, int]:
    """Delete raw response files past their retention. Returns (files, bytes).

    Separate retention from snapshots because these serve a different purpose:
    a snapshot is the record, a raw response is the receipt you keep briefly in
    case the parse looks wrong.
    """
    from pathlib import Path as _Path

    days = settings.raw_retention_days
    if days <= 0:
        return 0, 0
    directory = _Path(settings.raw_json_dir)
    if not directory.is_dir():
        return 0, 0

    import time

    cutoff = time.time() - days * 86400
    files = 0
    freed = 0
    for path in directory.glob("*.json"):
        try:
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            files += 1
            freed += stat.st_size
            if not dry_run:
                path.unlink()
        except OSError:
            continue
    return files, freed


async def slim_snapshots(
    session,
    slim_days: int,
    retention_days: int,
    *,
    now,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Drop the raw receipts from snapshot rows past the slim age.

    A snapshot row carries the record (every parsed price, promo, clearance,
    and inventory field) and the receipt it was parsed from (raw_json, ~1.9 KB
    against the record's few hundred bytes). Slimming nulls the receipt and
    keeps the record, so point-by-point history can outlive the receipts that
    produced it instead of being deleted with them.

    Owns the band between the delete cutoff and the slim cutoff: rows older
    than retention_days belong to the delete stage, so a slim age at or past
    the delete age does nothing (the caller warns). Returns (rows, bytes) —
    counted the same whether or not dry_run, and deliberately not gated by
    the uncaptured-items guard, because no price fact is lost here.
    """
    from datetime import timedelta

    from sqlalchemy import func, null, select, update

    from hd.db.models import StoreSnapshot

    if slim_days <= 0 or slim_days >= retention_days:
        return 0, 0

    slim_cutoff = now - timedelta(days=slim_days)
    delete_cutoff = now - timedelta(days=retention_days)
    in_band = (
        StoreSnapshot.ts < slim_cutoff,
        StoreSnapshot.ts >= delete_cutoff,
        StoreSnapshot.raw_json.isnot(None),
    )
    rows, size = (await session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(func.length(StoreSnapshot.raw_json)), 0),
        ).where(*in_band)
    )).one()
    if rows and not dry_run:
        # null(), not None: the JSON column type stores Python None as the
        # JSON text 'null', which IS NOT NULL and would be slimmed forever.
        await session.execute(
            update(StoreSnapshot).where(*in_band).values(raw_json=null())
        )
    return rows, size


@app.command()
def prune(
    days: int = typer.Option(0, help="Retention days (0 = use config)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show count without deleting"),
    force: bool = typer.Option(
        False, "--force", help="Prune even if price stats have not captured every item"
    ),
) -> None:
    """Delete old snapshot rows beyond retention period."""
    setup_logging()
    settings = Settings()

    async def _prune():
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, func, delete
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import ItemPriceStat, StoreSnapshot

        await _init_tables(settings)

        retention_days = days if days > 0 else settings.snapshot_retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        slim_days = settings.snapshot_slim_days

        try:
            async with get_session(settings) as session:
                count_result = await session.execute(
                    select(func.count()).select_from(StoreSnapshot).where(
                        StoreSnapshot.ts < cutoff
                    )
                )
                count = count_result.scalar() or 0

                # Deleting a snapshot is only safe once its price facts live in
                # item_price_stats; otherwise an item's entire recorded history
                # can vanish with nothing left to say it existed.
                # An anti-join, not a count comparison. item_price_stats is
                # designed to outlive the snapshots it was folded from, so once
                # anything has been pruned `captured` includes rows whose
                # snapshots are gone while `observed` shrinks — the difference
                # goes negative, max(0, ...) swallows it, and the guard silently
                # stops guarding exactly as the history it protects grows.
                seen = (
                    select(StoreSnapshot.store_id, StoreSnapshot.item_id)
                    .where(StoreSnapshot.price_value.isnot(None))
                    .distinct()
                    .subquery()
                )
                uncaptured = (await session.execute(
                    select(func.count()).select_from(seen).where(
                        ~select(ItemPriceStat)
                        .where(
                            ItemPriceStat.store_id == seen.c.store_id,
                            ItemPriceStat.item_id == seen.c.item_id,
                        )
                        .exists()
                    )
                )).scalar() or 0

                if dry_run:
                    slimmed, slim_bytes = await slim_snapshots(
                        session, slim_days, retention_days,
                        now=datetime.now(timezone.utc), dry_run=True,
                    )
                    return count, 0, uncaptured, slimmed, slim_bytes
                if uncaptured and not force:
                    # Slimming still runs: it loses no price fact, so the
                    # guard that blocks deletion has no claim over it.
                    slimmed, slim_bytes = await slim_snapshots(
                        session, slim_days, retention_days,
                        now=datetime.now(timezone.utc),
                    )
                    return count, -1, uncaptured, slimmed, slim_bytes

                if count > 0:
                    await session.execute(
                        delete(StoreSnapshot).where(StoreSnapshot.ts < cutoff)
                    )
                slimmed, slim_bytes = await slim_snapshots(
                    session, slim_days, retention_days, now=datetime.now(timezone.utc)
                )
                return count, count, uncaptured, slimmed, slim_bytes
        finally:
            await close_db()

    eligible, deleted, uncaptured, slimmed, slim_bytes = _run(_prune())
    raw_files, raw_bytes = prune_raw_responses(settings, dry_run=dry_run)

    # Deleting rows does not shrink a SQLite file; reclaim the space once
    # enough of it is waste to justify rewriting the database.
    from hd.db.base import maybe_vacuum

    vacuumed, vacuum_note = maybe_vacuum(
        settings.database_url, settings.vacuum_threshold_pct, dry_run=dry_run
    )
    style = "green" if vacuumed else "dim"
    console.print(f"[{style}]Database: {vacuum_note}[/{style}]")
    if raw_files:
        verb = "would free" if dry_run else "freed"
        console.print(
            f"[dim]Raw responses: {verb} {raw_files:,} file(s), "
            f"{raw_bytes/1e6:,.0f} MB (older than {settings.raw_retention_days}d)[/dim]"
        )
    retention_days = days if days > 0 else settings.snapshot_retention_days
    if 0 < retention_days <= settings.snapshot_slim_days:
        console.print(
            f"[yellow]SNAPSHOT_SLIM_DAYS ({settings.snapshot_slim_days}) is not "
            f"below the {retention_days}-day retention — slimming never fires.[/yellow]"
        )
    if slimmed:
        verb = "would drop" if dry_run else "dropped"
        console.print(
            f"[dim]Slimmed: {verb} receipts from {slimmed:,} snapshot(s), "
            f"{slim_bytes/1e6:,.0f} MB (older than {settings.snapshot_slim_days}d; "
            f"records kept)[/dim]"
        )
    if dry_run:
        console.print(f"[yellow]Dry run: {eligible} snapshots eligible for deletion.[/yellow]")
        if uncaptured:
            console.print(
                f"[red]{uncaptured} item(s) have price history not yet captured "
                f"in item_price_stats — run 'hd backfill-stats' first.[/red]"
            )
    elif deleted < 0:
        console.print(
            f"[red]Refusing to prune: {uncaptured} item(s) have price history that "
            f"only exists in store_snapshots.[/red]\n"
            f"Run [bold]hd backfill-stats[/bold] first, or pass --force to delete anyway."
        )
        raise typer.Exit(code=1)
    else:
        console.print(f"[green]Pruned {deleted} old snapshots.[/green]")


@app.command()
def backup(
    dest: list[str] = typer.Option(
        None, "--dest", help="Destination directory (repeatable; default: BACKUP_DIRS)"
    ),
    keep: int = typer.Option(0, help="Snapshots to keep per destination (0 = use config)"),
) -> None:
    """Write a verified snapshot of the database to each backup destination."""
    setup_logging()
    settings = Settings()

    from hd.db.base import backup_database

    dirs = list(dest) if dest else [
        d.strip() for d in settings.backup_dirs.split(",") if d.strip()
    ]
    if not dirs:
        console.print(
            "[red]No destination.[/red] Set BACKUP_DIRS in .env or pass --dest."
        )
        raise typer.Exit(code=1)

    keep_n = keep if keep > 0 else settings.backup_keep
    failures = 0
    for directory in dirs:
        path, message = backup_database(
            settings.database_url, directory, keep=keep_n
        )
        if path is None:
            failures += 1
            console.print(f"[yellow]{directory}: {message}[/yellow]")
        else:
            console.print(f"[green]{directory}: {message}[/green]")
    if failures == len(dirs):
        raise typer.Exit(code=1)


@app.command("backfill-stats")
def backfill_stats(
    chunk: int = typer.Option(50_000, help="Rows to read per batch"),
) -> None:
    """Rebuild item_price_stats from the raw snapshots that still exist."""
    setup_logging()
    settings = Settings()

    async def _backfill():
        from hd.db.base import init_db as _init_tables, close_db
        from hd.db.price_stats import backfill

        await _init_tables(settings)
        try:
            return await backfill(settings, chunk_size=chunk)
        finally:
            await close_db()

    scanned, items = _run(_backfill())
    console.print(
        f"[green]Captured {items:,} item(s) from {scanned:,} priced snapshots.[/green]"
    )
