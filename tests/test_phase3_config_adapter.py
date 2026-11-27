"""Phase 3: typed setup config and the retailer adapter seam."""

import pytest
from pydantic import ValidationError

from hd.envfile import EnvFile, SetupConfig, build_env_values, parse_selection


def test_wizard_still_exposes_envfile_helpers():
    from hd import setup_wizard as sw

    assert sw.EnvFile is EnvFile
    assert sw.parse_selection is parse_selection
    assert sw.build_env_values is build_env_values


def test_setup_config_accepts_a_good_config():
    cfg = SetupConfig.from_env_values(
        {"STORES": "1754,6978", "BRANDS": "Milwaukee", "BRAND_TOKENS": "Milwaukee=zv"}
    )
    assert cfg.stores == ["1754", "6978"]


@pytest.mark.parametrize(
    "values",
    [
        {"STORES": ""},
        {"STORES": "abc"},
        {"STORES": "1754", "BRANDS": "Milwaukee", "BRAND_TOKENS": ""},
        {"STORES": "1754", "SLACK_WEBHOOK_URL": "http://insecure.example"},
    ],
)
def test_setup_config_rejects_bad_values(values):
    with pytest.raises((ValidationError, ValueError)):
        SetupConfig.from_env_values(values)


# ── RetailerAdapter ───────────────────────────────────────────────────────────


def test_home_depot_satisfies_the_protocol():
    from hd.retailers import HomeDepotAdapter, RetailerAdapter, get_adapter

    assert isinstance(HomeDepotAdapter(), RetailerAdapter)
    assert get_adapter("homedepot").name == "homedepot"


def test_unknown_retailer_is_a_clear_error():
    from hd.retailers import get_adapter

    with pytest.raises(KeyError, match="unknown retailer"):
        get_adapter("nope")


def test_register_rejects_non_adapters():
    from hd.retailers import register_adapter

    with pytest.raises(TypeError):
        register_adapter(object())  # type: ignore[arg-type]


def test_adapter_parses_fulfillment_like_the_parser():
    from hd.hd_api.parsers import bopis_state
    from hd.retailers import get_adapter

    item = {"fulfillmentOptions": []}
    assert get_adapter().parse_fulfillment(item, "1754") == bopis_state(item, "1754")


def test_adapter_browse_delegates_to_graphql(monkeypatch):
    import asyncio

    from hd.hd_api import graphql
    from hd.retailers import get_adapter

    seen = {}

    async def fake_search(client, **kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(graphql, "search", fake_search)
    out = asyncio.run(get_adapter().browse_category(object(), store_id="1754", nav_param="N-abc"))
    assert out == {"ok": True}
    assert seen["store_id"] == "1754" and seen["nav_param"] == "N-abc"
