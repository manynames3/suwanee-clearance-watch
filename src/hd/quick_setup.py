"""Setup without questions, for an install whose stores are already known.

`hd setup` finds stores by ZIP, which costs a request that Home Depot may
throttle before anything is saved. When the store ids are known up front,
that lookup buys nothing: this writes them directly, resolves each brand to
its verified token, and installs the schedule.

It is safe to run again. Brands already in .env are kept and cost no request.
If Home Depot throttles partway, what was resolved is saved, the rest is
named, and the next run picks up only the brands still missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hd.config import Settings
from hd.hd_api.stores import StoreResult
from hd.setup_wizard import EnvFile, _format_wait, _setup_cooldown, seed_stores


@dataclass
class QuickSetupResult:
    stores: list[StoreResult] = field(default_factory=list)
    brands_kept: list[str] = field(default_factory=list)
    brands_added: list[str] = field(default_factory=list)
    brands_unknown: list[str] = field(default_factory=list)
    # Brands not looked up because Home Depot throttled or a cooldown is active.
    brands_pending: list[str] = field(default_factory=list)
    wait: str | None = None
    schedule: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when a scan has at least one store and one brand to read."""
        return bool(self.stores and (self.brands_kept or self.brands_added))


def parse_store(spec: str) -> StoreResult:
    """`1754` or `1754|Name|City|ST|ZIP` -> StoreResult.

    The long form gives the store a name on the deals page and a working
    store-page link; the short form still scans.
    """
    parts = [p.strip() for p in spec.split("|")]
    if not parts[0].isdigit():
        raise ValueError(f"{spec!r} is not a store id")
    parts += [None] * (5 - len(parts))
    store_id, name, city, state, zip_code = (p or None for p in parts[:5])
    return StoreResult(store_id=store_id, name=name, city=city, state=state, zip=zip_code)


def parse_brand_tokens(value: str | None) -> dict[str, str]:
    """BRAND_TOKENS (`Name:token,Name:token`) -> {name: token}, order kept."""
    out: dict[str, str] = {}
    for entry in (value or "").split(","):
        name, _, token = entry.strip().rpartition(":")
        if name and token:
            out[name] = token
    return out


async def run_quick_setup(
    root: Path,
    store_specs: list[str],
    brand_names: list[str],
    *,
    install_schedule: bool = True,
    resolver=None,
    client=None,
) -> QuickSetupResult:
    """Write stores and brands to .env and the database; install the schedule."""
    from hd.pipeline.brands import BrandResolutionError, BrandThrottled, resolve_brand

    result = QuickSetupResult(stores=[parse_store(s) for s in store_specs])
    if not result.stores:
        raise ValueError("at least one store is required")

    env = EnvFile.load(root / ".env")
    database_url = env.get("DATABASE_URL") or Settings().database_url
    settings = Settings(database_url=database_url, stores=",".join(s.store_id for s in result.stores))

    tokens = parse_brand_tokens(env.get("BRAND_TOKENS"))
    have = {name.upper() for name in tokens}
    wanted = []
    for name in (b.strip() for b in brand_names):
        if not name:
            continue
        if name.upper() in have:
            result.brands_kept.append(name)
        elif name.upper() not in {w.upper() for w in wanted}:
            wanted.append(name)

    cooldown = _setup_cooldown(root, settings)
    if wanted and cooldown.is_active():
        # An earlier request was throttled. Send nothing; say when to return.
        result.brands_pending = wanted
        result.wait = _format_wait(cooldown.remaining_seconds())
    elif wanted:
        resolver = resolver or resolve_brand
        owns_client = client is None
        if owns_client:
            from hd.http.client import HDClient

            client = HDClient(settings, request_budget=60)
        try:
            for i, name in enumerate(wanted):
                try:
                    match = await resolver(client, settings, name, result.stores[0].store_id)
                except BrandThrottled:
                    result.brands_pending = wanted[i:]
                    remaining = cooldown.remaining_seconds()
                    result.wait = _format_wait(remaining) if remaining > 0 else None
                    break
                except BrandResolutionError:
                    result.brands_pending = wanted[i:]
                    break
                if match is None:
                    result.brands_unknown.append(name)
                    continue
                tokens[match.name] = match.token
                result.brands_added.append(match.name)
        finally:
            if owns_client:
                await client.close()

    values = {"STORES": ",".join(s.store_id for s in result.stores), "DATABASE_URL": database_url}
    if tokens:
        # Written together, always: a brand without its token scans nothing.
        values["BRANDS"] = ",".join(tokens)
        values["BRAND_TOKENS"] = ",".join(f"{n}:{t}" for n, t in tokens.items())
    env.set_many(values)
    env.save()
    await seed_stores(Settings(database_url=database_url), result.stores)

    if install_schedule and result.ready:
        result.schedule = await install_scan_schedule(root)
    return result


async def install_scan_schedule(root: Path) -> list[tuple[str, bool, str]]:
    """Install and load the scan and prune jobs on macOS. Returns (name, ok, detail)."""
    from hd.setup_schedule import (
        hd_executable, is_macos, label_for, launch_agents_dir, load_agent,
        prune_slot, render_prune_plist, render_scan_plist, scan_slots, write_agent,
    )

    if not is_macos():
        return [("schedule", False, "not macOS; run `hd setup` for crontab lines")]
    hd_path = hd_executable()
    if hd_path is None:
        return [("schedule", False, "could not locate the `hd` command")]
    label = label_for()
    agents = launch_agents_dir()
    paths = [
        write_agent(agents / f"{label}.plist",
                    render_scan_plist(label, root, hd_path, scan_slots())),
        write_agent(agents / f"{label}.prune.plist",
                    render_prune_plist(f"{label}.prune", root, hd_path, prune_slot())),
    ]
    out = []
    for path in paths:
        ok, detail = await load_agent(path)
        out.append((path.name, ok, detail))
    return out
