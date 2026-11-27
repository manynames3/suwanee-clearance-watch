"""CLI entry point (Typer): `hd = "hd.cli:app"`.

Each command group lives in its own module and registers itself on the shared
`app` from `_common` when imported. The command bodies are thin: they parse
options, guard configuration, and hand off to the service modules in
`hd.pipeline` and friends.
"""

from __future__ import annotations

from hd.cli._common import (  # noqa: F401  re-exported for callers and tests
    _need_brand_tokens,
    _need_brands,
    _need_stores,
    _plugins,
    _post_run_hooks,
    _run,
    app,
    console,
)
from hd.cli import (  # noqa: F401  importing registers the commands
    maintenance_cmds,
    report_cmds,
    run_cmd,
    scan_cmds,
    setup_cmds,
)
from hd.cli.maintenance_cmds import prune_raw_responses, slim_snapshots  # noqa: F401

# Attach any plugin commands. Absent on a stock install.
_plugin_mod = _plugins()
if _plugin_mod is not None and hasattr(_plugin_mod, "register"):
    _plugin_mod.register(app)


if __name__ == "__main__":
    app()
