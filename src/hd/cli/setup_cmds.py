"""Setup and configuration commands."""

from __future__ import annotations

import asyncio
import typer
from hd.logging import setup_logging
from typing import Optional
from hd.cli._common import _run, app, console
from hd.config import Settings


@app.command()
def setup() -> None:
    """Interactive first-run setup: find your stores and brands, write .env."""
    from pathlib import Path

    from hd.setup_wizard import run_setup

    raise typer.Exit(_run(run_setup(Path.cwd())))


@app.command()
def init_db() -> None:
    """Create/migrate tables and seed default stores."""
    setup_logging()
    settings = Settings()

    async def _init():
        from hd.db.base import init_db as _init_tables, get_session, close_db
        from hd.db.models import Store

        await _init_tables(settings)

        async with get_session(settings) as session:
            from sqlalchemy import select

            for store_id in settings.store_list:
                result = await session.execute(
                    select(Store).where(Store.store_id == store_id)
                )
                if result.scalar_one_or_none() is None:
                    session.add(Store(store_id=store_id))

        await close_db()

    _run(_init())
    console.print(f"[green]Database initialized. Stores seeded: {settings.store_list}[/green]")


@app.command()
def add_store(
    store_id: str = typer.Argument(..., help="Store ID to add"),
    name: Optional[str] = typer.Option(None, help="Store name"),
    state: Optional[str] = typer.Option(None, help="Store state"),
    zip_code: Optional[str] = typer.Option(None, "--zip", help="Store ZIP code"),
    city: Optional[str] = typer.Option(None, help="Store city (defaults to name)"),
) -> None:
    """Add a store, or update the details of one that already exists."""
    setup_logging()
    settings = Settings()

    async def _add():
        from hd.db.base import get_session, close_db, init_db as _init_tables
        from hd.db.models import Store
        from sqlalchemy import select

        await _init_tables(settings)

        async with get_session(settings) as session:
            result = await session.execute(
                select(Store).where(Store.store_id == store_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                # Update in place: store location details are filled in after
                # the row is first seeded, and refusing here left them empty.
                updates = {"name": name, "state": state, "zip": zip_code, "city": city}
                applied = [k for k, v in updates.items() if v is not None]
                for key in applied:
                    setattr(existing, key, updates[key])
                await close_db()
                return "updated" if applied else "exists"

            session.add(Store(
                store_id=store_id,
                name=name,
                state=state,
                zip=zip_code,
                city=city,
            ))

        await close_db()
        return "added"

    result = _run(_add())
    if result == "added":
        console.print(f"[green]Store {store_id} added.[/green]")
    elif result == "updated":
        console.print(f"[green]Store {store_id} updated.[/green]")
    else:
        console.print(f"[yellow]Store {store_id} already exists — pass fields to update.[/yellow]")


@app.command("quick-setup")
def quick_setup(
    store: list[str] = typer.Option(
        ..., "--store", help="Store id, or 'id|Name|City|ST|ZIP'. Repeat for each store."
    ),
    brands: str = typer.Option(..., help="Brand names, comma separated."),
    schedule: bool = typer.Option(True, help="Install the recurring scan on macOS."),
) -> None:
    """Configure known stores and brands without questions. Safe to run again."""
    from pathlib import Path

    from rich.markup import escape

    from hd.quick_setup import run_quick_setup

    try:
        r = asyncio.run(run_quick_setup(
            Path.cwd(), store, brands.split(","), install_schedule=schedule
        ))
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)

    console.print("Stores: " + escape(", ".join(s.label for s in r.stores)))
    known = r.brands_kept + r.brands_added
    console.print("Brands: " + (escape(", ".join(known)) if known else "none yet"))
    for name in r.brands_unknown:
        console.print(f"[yellow]No brand called {escape(repr(name))} in Home Depot's catalog; skipped.[/yellow]")
    if r.brands_pending:
        when = f" in about {r.wait}" if r.wait else " later"
        console.print(
            "[yellow]Home Depot is rate limiting, so these brands were not looked up: "
            f"{escape(', '.join(r.brands_pending))}. Run this again{when}; "
            "only the missing ones will be requested.[/yellow]"
        )
    for name, ok, detail in r.schedule:
        if ok:
            console.print(f"[green]Scheduled[/green] {escape(name)}")
        else:
            console.print(f"[yellow]Schedule not installed ({escape(name)}): {escape(detail)}[/yellow]")
    if not r.ready:
        console.print("[yellow]Not ready to scan yet: no brand is configured.[/yellow]")
        raise typer.Exit(3)


@app.command()
def install_deals_poll(
    load: bool = typer.Option(
        False, "--load",
        help="Also register the job with launchd. Without it the plist is written and the load command printed.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plist instead of writing it"),
) -> None:
    """Write the launchd job that prices the Daily Deals set the minute it refreshes.

    The job runs `hd daily-deals --wait-for-refresh && hd notify` at 3:00
    Eastern, converted to this machine's clock. It is written but not loaded
    unless --load is given, so the schedule can be reviewed before a single
    request is made. Re-run after a daylight-saving change: the slot is
    computed for the date it is written.
    """
    from pathlib import Path

    from hd.setup_schedule import (
        deals_poll_slot,
        hd_executable,
        is_macos,
        label_for,
        launch_agents_dir,
        load_agent,
        render_deals_poll_plist,
        write_agent,
    )

    hd = hd_executable()
    if hd is None:
        console.print("[red]Could not find the `hd` executable beside the interpreter; "
                      "refusing to write a job that would fail at every slot.[/red]")
        raise typer.Exit(code=1)
    root = Path.cwd()
    slot = deals_poll_slot()
    if not is_macos():
        from hd.setup_schedule import _shell

        console.print("launchd jobs are macOS only. Add this line with `crontab -e` "
                      f"(it is 3:00 Eastern on this machine's clock):")
        console.print(
            f"{slot.minute} {slot.hour} * * * cd {_shell(root)} && {_shell(hd)} daily-deals "
            f"--wait-for-refresh && {_shell(hd)} notify",
            markup=False, highlight=False, soft_wrap=True,
        )
        raise typer.Exit(code=1)

    label = f"{label_for()}.dailydeals"
    contents = render_deals_poll_plist(label, root, hd, slot)
    if dry_run:
        # Plain text: Rich would wrap the XML at 80 columns when piped to a file.
        console.print(contents, markup=False, highlight=False, soft_wrap=True)
        if load:
            console.print("[dim]--dry-run: nothing written, so --load was ignored.[/dim]")
        return
    path = write_agent(launch_agents_dir() / f"{label}.plist", contents)
    console.print(f"[green]Wrote {path}[/green] — polls from {slot.hour:02d}:{slot.minute:02d} local (3:00 Eastern).")
    if load:
        ok, output = _run(load_agent(path))
        if not ok:
            console.print(f"[red]launchctl refused the job: {output}[/red]")
            raise typer.Exit(code=1)
        console.print("[green]Loaded.[/green] It fires at the next 3:00 Eastern; nothing runs now.")
    else:
        console.print(f"[dim]Not loaded. Activate with: launchctl load {path}[/dim]")


@app.command("publish-site")
def publish_site_cmd() -> None:
    """Write the phone deals page from the database; upload it if SITE_WORKER is set."""
    from rich.markup import escape

    from hd.config import Settings
    from hd.pipeline.site import publish_site

    settings = Settings()
    ok, message = asyncio.run(publish_site(settings, None))
    console.print(f"[{'green' if ok else 'red'}]{escape(message)}[/]")
    if not ok:
        raise typer.Exit(1)
