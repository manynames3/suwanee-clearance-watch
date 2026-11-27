"""The scheduled pipeline command: `hd run-once`."""

from __future__ import annotations

import typer
from hd.config import Settings
from hd.logging import setup_logging
from hd.cli._common import _need_brand_tokens, _need_stores, _post_run_hooks, _run, app, console


@app.command()
def run_once(
    mode: str = typer.Option("auto", help="Run mode: auto, full, snapshot-only"),
) -> None:
    """Run pipeline: snapshot -> diff -> alerts (+ discovery on full runs).

    Modes:
      auto          - snapshot-only by default; full discovery once daily at 00:00 UTC
      full          - discovery + snapshots (catalog refresh)
      snapshot-only - snapshots only (clearance/price monitoring)
    """
    setup_logging()
    settings = Settings()
    _need_stores(settings.store_list)
    _need_brand_tokens(settings)

    from hd.pipeline.runner import run_pipeline

    products, snapshots, alerts_count, sent, canvas_deals, effective_mode = _run(
        run_pipeline(settings, mode, console, _post_run_hooks)
    )
    if effective_mode == "cooldown":
        # Not a completed run; saying so plainly beats reporting zero of
        # everything as though the catalog were empty.
        from hd.http.cooldown import ThrottleCooldown

        remaining = ThrottleCooldown(
            settings.throttle_cooldown_path, settings.throttle_cooldown_seconds
        ).remaining_seconds()
        console.print(
            f"[yellow]Run deferred: Home Depot throttled an earlier run. "
            f"Resuming in {round(remaining / 60)} min.[/yellow]"
        )
        return
    msg = f"[green]Pipeline complete ({effective_mode}): "
    if products:
        msg += f"{products} products, "
    msg += f"{snapshots} snapshots, {alerts_count} alerts."
    if sent:
        msg += f" Sent {sent} group(s) to Slack."
    if canvas_deals:
        msg += f" Canvas: {canvas_deals} deal(s)."
    msg += "[/green]"
    console.print(msg)
