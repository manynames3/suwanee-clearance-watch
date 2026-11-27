"""Phase 4: the installable phone page."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hd.config import Settings
from hd.pipeline.site import ASSETS, DISCLAIMER, TEMPLATE_DIR, worth_the_drive, write_site


def row(saves, score, title="x", item="1"):
    return {"saves": saves, "confidence": {"score": score} if score is not None else None,
            "title": title, "item_id": item}


def test_worth_requires_enough_confident_deals_and_savings():
    s = Settings(min_stock_confidence=40, worth_drive_min_deals=3, worth_drive_min_savings=100)
    rows = [row(60, 80), row(50, 70, item="2"), row(10, 45, item="3")]
    w = worth_the_drive(rows, s)
    assert w["worth"] and w["deals"] == 3 and w["savings"] == 120.0
    assert w["best"]["saves"] == 60


def test_low_confidence_and_unscored_deals_do_not_earn_the_label():
    s = Settings(min_stock_confidence=40, worth_drive_min_deals=1, worth_drive_min_savings=10)
    assert not worth_the_drive([row(500, 10), row(500, None)], s)["worth"]


def test_unknown_savings_are_not_counted():
    s = Settings(worth_drive_min_deals=1, worth_drive_min_savings=1)
    assert worth_the_drive([row(None, 90)], s)["deals"] == 0


def test_empty_store_is_not_worth_it():
    w = worth_the_drive([], Settings())
    assert not w["worth"] and w["best"] is None


def test_write_site_ships_the_pwa_shell_and_stamps_the_service_worker(tmp_path):
    s = Settings(site_out_dir=str(tmp_path / "out"))
    out = write_site(s, {"run": {}, "stores": []})
    for name in ASSETS:
        assert (out / name).exists(), name
    sw = (out / "sw.js").read_text()
    assert "__BUILD__" not in sw and "clearance-20" in sw
    assert "/sw.js" in (out / "_headers").read_text()


def test_manifest_is_installable():
    m = json.loads((TEMPLATE_DIR / "manifest.webmanifest").read_text())
    assert m["display"] == "standalone" and m["start_url"] and m["name"]
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in m["icons"]:
        assert (TEMPLATE_DIR / icon["src"]).exists()


def test_service_worker_only_handles_same_origin_gets():
    sw = (TEMPLATE_DIR / "sw.js").read_text()
    assert "url.origin !== location.origin" in sw and 'req.method !== "GET"' in sw


def test_page_registers_worker_and_links_manifest():
    html = (TEMPLATE_DIR / "index.html").read_text()
    for needle in ('rel="manifest"', "serviceWorker", "barcode.js", "Worth the drive?", "Hunter card"):
        assert needle in html
    # Links only ever point at Home Depot or Google Maps.
    assert "innerHTML" not in html


def test_disclaimer_is_published():
    assert "Verify in store" in DISCLAIMER


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
class TestBarcodeJS:
    def run_js(self, code: str) -> str:
        script = f"const b=require({json.dumps(str(TEMPLATE_DIR / 'barcode.js'))});{code}"
        return subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout.strip()

    def test_normalize_validates_check_digit_and_length(self):
        out = self.run_js(
            'console.log(JSON.stringify([b.normalize("036000291452"),b.normalize("036000291453"),'
            'b.normalize("0036000291452"),b.normalize("00036000291452"),b.normalize("12"),b.normalize(null)]))'
        )
        assert json.loads(out) == ["0036000291452", None, "0036000291452", "0036000291452", None, None]

    def test_modules_are_95_wide_with_guards(self):
        m = self.run_js('console.log(b.modules("0036000291452"))')
        assert len(m) == 95
        assert m.startswith("101") and m.endswith("101") and m[45:50] == "01010"

    def test_ean13_and_upca_agree_for_a_leading_zero(self):
        a = self.run_js('console.log(b.modules(b.normalize("036000291452")))')
        b = self.run_js('console.log(b.modules(b.normalize("0036000291452")))')
        assert a == b

    def test_known_upc_a_left_half(self):
        # UPC-A 036000291452: digits 0,3,6,0,0,0 in L-code after the start guard.
        m = self.run_js('console.log(b.modules("0036000291452"))')
        l_codes = ["0001101", "0111101", "0101111", "0001101", "0001101", "0001101"]
        assert m[3:45] == "".join(l_codes)
