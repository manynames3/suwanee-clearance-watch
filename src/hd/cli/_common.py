"""Shared CLI plumbing: the Typer app, console, plugin seam and guards."""

from __future__ import annotations

import asyncio
import typer
from hd.config import Settings
from rich.console import Console


app = typer.Typer(name="hd", help="Home Depot Clearance Monitor")


console = Console()


def _plugins():
    """The optional local plugin module, or None on a stock install.

    A convention rather than a registry: if an `hd.plugins` package is present
    alongside this one it may expose `register(app)` to attach extra commands
    and `post_run_hooks()` to do work after a scan completes. Nothing ships
    with the scanner, and nothing here depends on one existing.
    """
    try:
        from hd import plugins
    except ImportError:
        return None
    return plugins


def _post_run_hooks() -> list:
    """(name, coroutine function) pairs to run after a completed scan."""
    mod = _plugins()
    getter = getattr(mod, "post_run_hooks", None) if mod is not None else None
    return list(getter()) if getter is not None else []


def _run(coro):
    """Run an async coroutine from sync CLI context."""
    return asyncio.run(coro)


def _need_stores(store_ids: list[str]) -> list[str]:
    """Stop unless there is at least one store to work on.

    Checked against the resolved list rather than the config, so an explicit
    `--stores` still works on an install that has never been configured. The
    shipped defaults are empty on purpose — a default store id would point a
    stranger's install at somebody else's neighbourhood — so the unconfigured
    case has to say so instead of succeeding against an empty list.
    """
    if not store_ids:
        console.print("[red]No stores configured.[/red] Run [cyan]hd setup[/cyan] first.")
        raise typer.Exit(1)
    return store_ids


def _need_brands(brands: list[str]) -> list[str]:
    """Stop unless there is at least one brand name to scan."""
    if not brands:
        console.print("[red]No brands configured.[/red] Run [cyan]hd setup[/cyan] first.")
        raise typer.Exit(1)
    return brands


def _need_brand_tokens(settings: Settings) -> None:
    """Stop when browse mode has no facet tokens to walk.

    Browse walks only brands present in brand_tokens, so without them the run
    succeeds having scanned nothing — the failure this guard exists to prevent.
    Discovery does not use tokens at all and is deliberately not gated on them.
    """
    if settings.browse_enabled and not settings.brand_token_list:
        console.print(
            "[red]No brand tokens configured.[/red] Browse mode walks only brands with a "
            "facet token, so this would scan nothing. Run [cyan]hd setup[/cyan] first."
        )
        raise typer.Exit(1)
