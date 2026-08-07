"""Render the board with sim drives and save a viewport screenshot.

  python tools/board_screenshot.py
  → reports/board-layout.png
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "board-layout.png"
PORT = int(os.environ.get("DRIVESTATION_PORT", "8339"))
BASE = f"http://127.0.0.1:{PORT}"


def _post(path: str, body: dict) -> None:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        res.read()


def _wait() -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(BASE + "/api/state", timeout=1) as res:
                if res.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    raise SystemExit("server did not start")


def main() -> int:
    env = os.environ.copy()
    env["DRIVESTATION_MODE"] = "sim"
    env["DRIVESTATION_PORT"] = str(PORT)
    env["DRIVESTATION_DB"] = str(ROOT / "reports" / "board-shot.db")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "drivestation.web.app:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait()
        inserts = [
            ("NVME-A1", "healthy_nvme"),
            ("NVME-B1", "worn_nvme"),
            ("SATA-1", "healthy_ssd"),
            ("SATA-2", "healthy_hdd"),
            ("SATA-3", "healthy_ssd"),
            ("SATA-4", "warning_hdd"),
            ("M2-1", "healthy_ssd"),
            ("SUITOK-1", "healthy_nvme"),
            ("SUITOK-2", "healthy_nvme"),
        ]
        for slot, preset in inserts:
            _post("/api/sim/insert", {"slot": slot, "preset": preset})
        time.sleep(1.2)  # identify threads → READY

        from playwright.sync_api import sync_playwright

        OUT.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(BASE + "/", wait_until="networkidle")
            # Kiosk has no sim panel — hide it so band heights match production.
            page.add_style_tag(content="#simpanel{display:none!important}")
            page.wait_for_selector(".metrics .fact", timeout=5000)
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT), full_page=False)
            for name, sel in (
                ("board-nvme-band.png", ".band:nth-of-type(1)"),
                ("board-sata-band.png", ".band:nth-of-type(2)"),
            ):
                loc = page.locator(sel)
                if loc.count():
                    loc.first.screenshot(path=str(OUT.parent / name))
            text = page.locator(".band:nth-of-type(1)").inner_text()
            for needle in ("POWERED", "CYCLES", "WRITTEN", "READ", "TEMP"):
                if needle not in text.upper():
                    raise SystemExit(f"NVMe band missing on-screen field: {needle}")
            browser.close()
        print(f"Wrote {OUT}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
