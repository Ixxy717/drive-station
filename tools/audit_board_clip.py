"""Fail if any occupied tile clips metrics or action buttons."""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "reports"
KEYS = ["POWERED", "CYCLES", "WRITTEN", "READ", "TEMP", "SPARE"]
URL = "http://127.0.0.1:8330/?v=dense"


def overlaps_or_outside(inner, outer, tol=1.0) -> bool:
    if not inner or not outer:
        return True
    return (
        inner["y"] + tol < outer["y"]
        or inner["x"] + tol < outer["x"]
        or inner["y"] + inner["height"] > outer["y"] + outer["height"] + tol
        or inner["x"] + inner["width"] > outer["x"] + outer["width"] + tol
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(URL, wait_until="networkidle")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(700)
        tiles = page.locator(".tile.solid")
        n = tiles.count()
        print(f"solid tiles: {n}")
        for i in range(n):
            t = tiles.nth(i)
            sid = t.locator(".tilebar .id").inner_text().strip()
            text = t.inner_text().upper()
            tb = t.bounding_box()
            for k in KEYS:
                if k not in text:
                    errors.append(f"{sid}: missing {k}")
            for sel in [".metrics", ".ask", ".method", ".idrow"]:
                el = t.locator(sel)
                if not el.count():
                    if sel == ".ask":
                        continue
                    errors.append(f"{sid}: missing {sel}")
                    continue
                box = el.first.bounding_box()
                if overlaps_or_outside(box, tb):
                    errors.append(f"{sid}: {sel} clipped {box} vs tile {tb}")
            facts = t.locator(".metrics .fact .v")
            for j in range(facts.count()):
                fb = facts.nth(j).bounding_box()
                if overlaps_or_outside(fb, tb):
                    errors.append(f"{sid}: metric value[{j}] clipped")
            btns = t.locator("button.act")
            for j in range(btns.count()):
                bb = btns.nth(j).bounding_box()
                if overlaps_or_outside(bb, tb):
                    errors.append(f"{sid}: button[{j}] clipped")
                if not btns.nth(j).is_visible():
                    errors.append(f"{sid}: button[{j}] not visible")
        page.screenshot(path=str(OUT / "local-dense.png"))
        browser.close()
    if errors:
        print("FAIL")
        for e in errors:
            print(" ", e)
        raise SystemExit(1)
    print("PASS — no clip; all metric keys present")
    print("wrote", OUT / "local-dense.png")


if __name__ == "__main__":
    main()
