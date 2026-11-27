"""The page-processing rules, tested without a client or a database."""

from __future__ import annotations

from types import SimpleNamespace as NS

from hd.pipeline import browse, fetcher, nav_planner, normalizer, persister


def prod(item_id, brand):
    return NS(item_id=item_id, brand=brand)


def test_note_coverage_tracks_new_ids_before_brand_filter():
    seen: set[str] = set()
    assert normalizer.note_coverage([prod("1", "A"), prod("2", None)], seen) is True
    assert seen == {"1", "2"}
    assert normalizer.note_coverage([prod("1", "A")], seen) is False


def test_select_products_respects_brand_set_and_all_brands():
    parsed = [prod("1", "Milwaukee"), prod("2", "Other"), prod("3", None)]
    assert [p.item_id for p in normalizer.select_products(parsed, False, {"MILWAUKEE"})] == ["1"]
    assert [p.item_id for p in normalizer.select_products(parsed, True, set())] == ["1", "2"]


def test_select_snapshots_skips_already_seen(sample_response):
    from hd.hd_api.parsers import parse_snapshots

    everything = parse_snapshots(sample_response, "2619")
    ids = {s.item_id for s in everything}
    first = everything[0].item_id
    picked = normalizer.select_snapshots(sample_response, "2619", ids, {first})
    assert first not in {s.item_id for s in picked}
    assert len(picked) == len(everything) - 1


def test_browse_remains_a_working_facade():
    for name in ("Walk", "BrowseSummary", "plan_walks", "price_partition", "run_browse",
                 "fetch_facets", "walk_and_capture", "coverage_memory", "_record_run_start",
                 "_record_run_end", "current_et_hour", "build_nav"):
        assert hasattr(browse, name), name
    assert browse.plan_walks is nav_planner.plan_walks
    assert browse.walk_and_capture is fetcher.walk_and_capture
    assert browse.coverage_memory is persister.coverage_memory


def test_the_split_modules_are_small_and_acyclic():
    import ast
    from pathlib import Path

    base = Path(browse.__file__).parent
    order = {"nav_planner": 0, "normalizer": 0, "fetcher": 1, "persister": 1, "browse": 2}
    for name, level in order.items():
        text = (base / f"{name}.py").read_text()
        assert len(text.splitlines()) < 1000, f"{name} is too large"
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("hd.pipeline."):
                target = node.module.rsplit(".", 1)[1]
                if target in order:
                    assert order[target] < level or target == name, f"{name} -> {target}"
