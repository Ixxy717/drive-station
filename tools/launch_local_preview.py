"""Launch local sim board with sample drives for layout preview.

Does NOT touch the mini PC.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("DRIVESTATION_PORT", "8330"))
BASE = f"http://127.0.0.1:{PORT}"


def post(path: str, body: dict) -> None:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        res.read()


def wait() -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/state", timeout=1) as res:
                if res.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.15)
    raise SystemExit("server failed to start")


def main() -> int:
    env = os.environ.copy()
    env["DRIVESTATION_MODE"] = "sim"
    env["DRIVESTATION_PORT"] = str(PORT)
    env["DRIVESTATION_DB"] = str(ROOT / "reports" / "local-preview.db")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "drivestation.web.app:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
    )
    try:
        wait()
        inserts = [
            ("NVME-A1", "healthy_nvme"),
            ("NVME-B1", "worn_nvme"),
            ("SATA-1", "healthy_ssd"),
            ("SATA-2", "healthy_hdd"),
            ("SATA-3", "healthy_ssd"),
            ("SATA-4", "warning_hdd"),
            ("M2-1", "healthy_ssd"),
            ("SUITOK-1", "healthy_nvme"),
            ("SUITOK-2", "worn_nvme"),
        ]
        for slot, preset in inserts:
            try:
                post("/api/sim/insert", {"slot": slot, "preset": preset})
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
        time.sleep(1.0)
        print(f"Local preview: {BASE}/")
        print("Sim panel at bottom — insert/yank freely.")
        print("Ctrl+C in this terminal stops the server.")
        webbrowser.open(f"{BASE}/")
        proc.wait()
        return proc.returncode or 0
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
