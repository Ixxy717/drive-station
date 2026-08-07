"""Live board audit screenshots + text dump."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "reports"
BASE = "http://192.168.1.200:8330"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    st = json.load(urllib.request.urlopen(f"{BASE}/api/state"))
    print("=== API ===")
    for s in st["slots"]:
        d = s.get("drive") or {}
        h = s.get("health") or {}
        dets = [x["k"] for x in (h.get("details") or [])]
        model = (d.get("model") or "")[:48]
        print(
            f"{s['slot_id']:10} {s['status']:10} "
            f"details={dets} model={model}"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"{BASE}/?t=audit", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "audit-full.png"))
        names = ["audit-nvme.png", "audit-sata.png", "audit-suitok.png"]
        for i, name in enumerate(names):
            band = page.locator(".band").nth(i)
            band.screenshot(path=str(OUT / name))
            print(f"\n=== UI {name} ===")
            print(band.inner_text())
        browser.close()
    print(f"\nWrote {OUT / 'audit-full.png'}")


if __name__ == "__main__":
    main()
