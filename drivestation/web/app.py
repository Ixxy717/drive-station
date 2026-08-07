from __future__ import annotations

import csv
import io
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, PlainTextResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..db import JobLog
from ..hw.simulator import (SimFaults, SimulatorBackend, make_hdd, make_nvme,
                            make_sata_ssd)
from ..models import SLOT_LAYOUT
from ..station import Station

log = logging.getLogger("drivestation")

MODE = os.environ.get("DRIVESTATION_MODE", "sim")
# Shown on certificates of data destruction. Set to the business name.
ORG = os.environ.get("DRIVESTATION_ORG", "Drive Station")
STATIC = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = Path(os.environ.get("DRIVESTATION_REPORTS", REPO_ROOT / "reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _lan_urls(port: int = 8330) -> list[str]:
    urls = [f"http://127.0.0.1:{port}/"]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            urls.append(f"http://{ip}:{port}/")
    except OSError:
        pass
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    port = int(os.environ.get("DRIVESTATION_PORT", "8330"))
    print()
    print("Drive Station is on the LAN — open from this PC or another:")
    for u in _lan_urls(port):
        print(f"  Grade  : {u}              (main board — StarTech etc.)")
        print(f"  Wipe   : {u}wipe          (WIPE ONLY docks / 2nd monitor)")
        print(f"  Logs   : {u}logs")
        print(f"  Drive  : {u}d/<SERIAL>   (eBay listing card)")
        print(f"  Files  : {u}files/")
    print("  Tip: set hostname 'drivestation' + Avahi for http://drivestation.local:8330/d/<SERIAL>")
    print()
    yield


app = FastAPI(title="drive-station", lifespan=lifespan)

if MODE == "real":
    from ..hw.linux import LinuxBackend
    slots_env = os.environ.get("DRIVESTATION_SLOTS")
    backend = LinuxBackend(
        slots_path=Path(slots_env) if slots_env else None,
        use_pyudev=os.environ.get("DRIVESTATION_NO_PYUDEV") != "1",
    )
else:
    backend = SimulatorBackend([s for s, _ in SLOT_LAYOUT])

joblog = JobLog(os.environ.get("DRIVESTATION_DB", "drivestation.db"))
station = Station(backend, joblog)

# Characterization reports + any other station files — same origin as the board.
app.mount("/files", StaticFiles(directory=str(REPORTS_DIR), html=True), name="files")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/wipe")
def wipe_board() -> FileResponse:
    """Second-monitor board: WIPE ONLY docks (SUITOK) + queued serials."""
    return FileResponse(STATIC / "index.html")


@app.get("/logs")
def logs_page() -> FileResponse:
    return FileResponse(STATIC / "logs.html")


@app.get("/d/{serial}")
@app.get("/s/{serial}")
def drive_page(serial: str) -> FileResponse:
    """Per-serial listing card for eBay / resale (open on phone or PC)."""
    rows = joblog.by_serial(serial)
    if not rows:
        raise HTTPException(status_code=404, detail="No wipe record for that serial")
    return FileResponse(STATIC / "drive.html")


@app.get("/cert/{serial}")
def cert_page(serial: str) -> FileResponse:
    """Printable certificate of data destruction (NIST SP 800-88 wording)."""
    rows = joblog.by_serial(serial)
    if not rows:
        raise HTTPException(status_code=404, detail="No wipe record for that serial")
    return FileResponse(STATIC / "cert.html")


@app.get("/api/state")
def state() -> dict:
    return {
        "sim_mode": MODE != "real",
        "slots": station.snapshot(),
        "pending_wipes": station.pending_wipes(),
    }


@app.get("/api/debug/hw")
def hw_debug() -> dict:
    """Evidence ladder: USB disks vs allowlist vs station presence."""
    busy = [
        s["slot_id"] for s in station.snapshot()
        if s["status"] in ("WIPING", "VERIFYING")
    ]
    payload: dict = {
        "mode": MODE,
        "active_wipes": station.active_wipes(),
        "busy_slots": busy,
        "pending_wipes": station.pending_wipes(),
    }
    dbg = getattr(backend, "hw_debug", None)
    if callable(dbg):
        payload.update(dbg())
    else:
        payload["note"] = "simulator backend — no live USB map"
    return payload


@app.get("/api/debug/kiosk", response_class=PlainTextResponse)
def kiosk_debug_dump() -> str:
    """Latest sudo debugkiosk dump — readable from the LAN after a black screen."""
    candidates = [
        Path("/tmp/ds-kiosk-debug.txt"),
        REPORTS_DIR / "kiosk-debug.txt",
        REPO_ROOT / "reports" / "kiosk-debug.txt",
    ]
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    raise HTTPException(
        status_code=404,
        detail="No debug dump yet — run: sudo debugkiosk",
    )


@app.get("/api/config")
def config() -> dict:
    return {"org": ORG, "sim_mode": MODE != "real", "batch": station.batch}


class BatchSet(BaseModel):
    batch: Optional[str] = None


@app.post("/api/batch")
def set_batch(req: BatchSet) -> dict:
    """Set the active batch/lot label; stamped onto every new wipe job."""
    station.batch = (req.batch or "").strip() or None
    return {"ok": True, "batch": station.batch}


@app.get("/api/records/{serial}/qr.svg")
def record_qr(serial: str, request: Request) -> Response:
    """QR code pointing at the drive's online wipe record (/d/SERIAL)."""
    try:
        import segno
    except ImportError:
        raise HTTPException(status_code=501,
                            detail="segno not installed (pip install segno)")
    if not joblog.by_serial(serial):
        raise HTTPException(status_code=404, detail="No record for that serial")
    url = f"{request.base_url}d/{serial}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=4, border=1)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/slots/{slot_id}/wipe")
def wipe(slot_id: str) -> dict:
    try:
        station.confirm_wipe(slot_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/slots/{slot_id}/wipe-here")
def wipe_here(slot_id: str) -> dict:
    """Force local wipe (even for ≥1TB NVMe on a grading dock)."""
    try:
        station.confirm_wipe_here(slot_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/slots/{slot_id}/queue")
def queue_wipe(slot_id: str) -> dict:
    """Queue serial for a WIPE ONLY dock; nothing destroyed on this bay."""
    try:
        station.queue_wipe(slot_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/slots/{slot_id}/decline")
def decline(slot_id: str) -> dict:
    station.decline_wipe(slot_id)
    return {"ok": True}


@app.get("/api/records")
def records(
    serial: Optional[str] = None,
    batch: Optional[str] = None,
    limit: int = Query(100, ge=1, le=2000),
) -> dict:
    if serial:
        rows = joblog.by_serial(serial)
    elif batch:
        rows = joblog.by_batch(batch, limit)
    else:
        rows = joblog.recent(limit)
    return {"records": rows}


@app.get("/api/records.csv")
def records_csv(limit: int = Query(2000, ge=1, le=10000)) -> StreamingResponse:
    rows = joblog.recent(limit)
    buf = io.StringIO()
    fields = [
        "id", "created_at", "slot", "manufacturer", "model", "serial",
        "capacity_bytes", "drive_type", "health_percent", "health_verdict",
        "health_warnings", "wipe_method", "wipe_started_at", "wipe_finished_at",
        "result", "error", "batch", "used_bytes_before", "usage_label",
    ]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=drivestation-jobs.csv"},
    )


@app.get("/api/records/{serial}/blurb", response_class=PlainTextResponse)
def blurb(serial: str) -> str:
    """Listing-ready text for resale descriptions (eBay etc.)."""
    rows = joblog.by_serial(serial)
    if not rows:
        raise HTTPException(status_code=404, detail="No record for that serial")
    r = rows[0]
    capacity_gb = (r["capacity_bytes"] or 0) / 1_000_000_000
    capacity = (f"{capacity_gb / 1000:.0f}TB" if capacity_gb >= 1000
                else f"{capacity_gb:.0f}GB")
    kind = {"NVME": "NVMe SSD", "SATA_SSD": "SATA SSD", "SATA_HDD": "SATA HDD",
            "SAS_HDD": "SAS HDD", "SAS_SSD": "SAS SSD"}.get(r["drive_type"], "drive")
    lines = [f"{r['manufacturer']} {r['model']} {capacity} {kind}"]
    if r["health_percent"] is not None:
        lines.append(f"Health: {r['health_percent']}% remaining")
    verdict = r["health_verdict"] or "UNKNOWN"
    if verdict in ("SCRAP", "FAIL"):
        lines.append(f"Grade: SCRAP — not for resale ({verdict})")
    else:
        lines.append(f"SMART/health check: {verdict}")
    if r.get("usage_label"):
        lines.append(f"Before wipe: {r['usage_label']}")
    if r["result"] == "PASSED":
        date = (r["wipe_finished_at"] or "")[:10]
        lines.append(f"Securely erased ({r['wipe_method'].replace('_', ' ').title()}) "
                     f"on {date} and verified empty")
    else:
        lines.append(f"WIPE RESULT: {r['result']} — not cleared for resale")
    lines.append(f"Serial: {r['serial']} (traceable wipe record on file)")
    return "\n".join(lines)


@app.get("/api/files")
def list_files() -> dict:
    """Index of files under reports/ for the logs page."""
    files = []
    if REPORTS_DIR.is_dir():
        for p in sorted(REPORTS_DIR.rglob("*")):
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            rel = p.relative_to(REPORTS_DIR).as_posix()
            files.append({
                "path": rel,
                "size_kb": max(1, p.stat().st_size // 1024),
            })
    return {"files": files[-500:]}  # newest-ish by walk order; cap size


@app.get("/api/urls")
def urls() -> dict:
    port = int(os.environ.get("DRIVESTATION_PORT", "8330"))
    return {"urls": _lan_urls(port)}


# -- simulator control endpoints (dev only) ----------------------------------

class SimInsert(BaseModel):
    slot: str
    preset: str


_PRESETS = {
    "healthy_nvme": lambda: make_nvme(percentage_used=6),
    "worn_nvme": lambda: make_nvme(percentage_used=85),
    "failing_nvme": lambda: make_nvme(percentage_used=20, media_errors=12,
                                      critical_warning=1),
    "healthy_ssd": lambda: make_sata_ssd(percent_life=92),
    "smart_fail_ssd": lambda: make_sata_ssd(smart_passed=False),
    "healthy_hdd": lambda: make_hdd(),
    "warning_hdd": lambda: make_hdd(reallocated=8),
    "failing_hdd": lambda: make_hdd(pending=5, uncorrectable=3),
    "yank_mid_wipe": lambda: make_nvme(faults=SimFaults(disconnect_at=0.4)),
    "wipe_rejected": lambda: make_sata_ssd(faults=SimFaults(wipe_rejected=True)),
    "verify_fail": lambda: make_hdd(faults=SimFaults(verify_fails=True)),
    "wont_identify": lambda: make_nvme(faults=SimFaults(identify_fails=True)),
}


@app.post("/api/sim/insert")
def sim_insert(req: SimInsert) -> dict:
    if MODE == "real":
        raise HTTPException(status_code=403, detail="Not in simulator mode")
    if req.preset not in _PRESETS:
        raise HTTPException(status_code=400, detail="Unknown preset")
    if backend.has_drive(req.slot):
        raise HTTPException(status_code=409, detail="Slot already occupied")
    backend.insert_drive(req.slot, _PRESETS[req.preset]())
    return {"ok": True}


@app.post("/api/sim/remove")
def sim_remove(req: dict) -> dict:
    if MODE == "real":
        raise HTTPException(status_code=403, detail="Not in simulator mode")
    backend.remove_drive(req["slot"])
    return {"ok": True}
