from pathlib import Path
import json
import urllib.request

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "reports"


def probe(url: str) -> None:
    try:
        st = json.load(urllib.request.urlopen(url, timeout=2))
        models = [
            (
                s["slot_id"],
                s["status"],
                ((s.get("drive") or {}).get("model") or "")[:28],
            )
            for s in st["slots"]
        ]
        print(url)
        print("  sim_mode=", st.get("sim_mode"))
        print("  slots=", models[:8])
    except Exception as e:
        print(url, "FAIL", e)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    probe("http://127.0.0.1:8330/api/state")
    probe("http://192.168.1.200:8330/api/state")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto("http://127.0.0.1:8330/", wait_until="networkidle")
        page.wait_for_timeout(1000)
        html = page.content()
        print("GRADE_METRICS in page:", "GRADE_METRICS" in html)
        print("suitok 2-col band:", "WIPE — SUITOK" in page.inner_text("body"))
        print("--- local NVME band ---")
        print(page.locator(".band").nth(0).inner_text()[:500])
        print("--- local SUITOK band ---")
        print(page.locator(".band").nth(2).inner_text()[:500])
        page.screenshot(path=str(OUT / "local-now.png"))
        browser.close()
    print("wrote", OUT / "local-now.png")


if __name__ == "__main__":
    main()
