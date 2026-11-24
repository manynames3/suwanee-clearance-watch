"""Phone deals page — a static page plus one JSON file, published after a run.

The scanner stays on this machine. After each run it writes the current deal
board and the run's health to deals.json, beside a static index.html, and
uploads that folder to Cloudflare as a static-assets Worker with wrangler. The page
never talks to Home Depot and holds no credentials.

A run that did not see everything publishes its reasons, and the page shows
them above the deals. Publishing is a convenience and never fails a scan.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hd.config import Settings
from hd.logging import get_logger
from hd.pipeline.digest import RunReport

log = get_logger("pipeline.site")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "site_template"

# Keeps the page out of search engines and stops phones caching old deals.
_HEADERS = """/*
  X-Robots-Tag: noindex, nofollow
/deals.json
  Cache-Control: no-store
"""


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:  # SQLite hands timestamps back naive; they are UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _deal_row(deal: dict[str, Any]) -> dict[str, Any]:
    price = deal.get("clearance_value")
    was = deal.get("online_price")
    known = price is not None and was is not None and was > price
    return {
        "item_id": deal.get("item_id"),
        "title": deal.get("title"),
        "url": deal.get("url"),
        "image_url": deal.get("image_url"),
        "price": price,
        "was": was if known else None,
        "pct_off": round((was - price) / was * 100) if known else None,
        "saves": round(was - price, 2) if known else None,
        "qty": deal.get("qty"),
        "in_stock": bool(deal.get("in_stock")),
        "is_new": bool(deal.get("is_new")),
        "retrieved_at": _iso(deal.get("snapshot_ts")),
    }


async def build_site_data(settings: Settings, report: RunReport | None) -> dict[str, Any]:
    """Everything the page shows: the run's health and each store's deals."""
    from hd.dashboard.queries import get_deal_board

    board = await get_deal_board(settings)
    floor = settings.min_dollar_off
    stores = []
    for store_id in settings.store_list:
        rows = [_deal_row(d) for d in board["stores"].get(store_id, [])]
        # Same floor the alerts use. An unknown saving is kept, not hidden.
        rows = [r for r in rows if r["saves"] is None or r["saves"] >= floor]
        rows.sort(key=lambda r: (-(r["saves"] or 0), r["title"] or ""))
        stores.append({
            "id": store_id,
            "name": board["store_names"].get(store_id),
            "deals": rows,
        })
    run: dict[str, Any] = {"started": None, "degraded": [], "notes": [],
                           "snapshots": 0, "requests": 0, "mode": ""}
    if report is not None:
        run = {
            "started": _iso(report.started),
            "mode": report.mode,
            "snapshots": report.snapshots,
            "requests": report.requests,
            "degraded": list(report.degraded),
            "notes": list(report.notes),
        }
    return {"run": run, "min_dollar_off": floor, "stores": stores}


def write_site(settings: Settings, data: dict[str, Any]) -> Path:
    """Write index.html, deals.json and _headers into the output folder."""
    out = Path(settings.site_out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_DIR / "index.html", out / "index.html")
    (out / "_headers").write_text(_HEADERS)
    (out / "deals.json").write_text(json.dumps(data, indent=1))
    return out


async def deploy_site(settings: Settings, out: Path) -> tuple[bool, str]:
    """Upload the folder to Cloudflare as a static-assets Worker. Returns (ok, message)."""
    npx = shutil.which("npx")
    if npx is None:
        return False, "npx was not found on PATH; install Node.js to publish the page"
    proc = await asyncio.create_subprocess_exec(
        npx, "--yes", "wrangler", "deploy",
        "--name", settings.site_worker,
        "--assets", str(out),
        "--compatibility-date", "2026-01-01",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), settings.site_deploy_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"wrangler did not finish within {settings.site_deploy_timeout:.0f}s"
    text = output.decode(errors="replace").strip()
    if proc.returncode != 0:
        return False, text[-400:] or f"wrangler exited with {proc.returncode}"
    return True, text.splitlines()[-1] if text else "deployed"


async def publish_site(settings: Settings, report: RunReport | None) -> tuple[bool, str]:
    """Build, write and (when a Worker name is set) upload the deals page."""
    data = await build_site_data(settings, report)
    out = write_site(settings, data)
    if not settings.site_worker:
        return True, f"written to {out} (SITE_WORKER is not set, so not uploaded)"
    ok, message = await deploy_site(settings, out)
    if ok:
        log.info("Deals page published", worker=settings.site_worker)
    else:
        log.error("Deals page could not be published", detail=message)
    return ok, message
