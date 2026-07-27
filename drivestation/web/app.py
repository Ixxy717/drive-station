from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..db import JobLog
from ..hw.simulator import (SimFaults, SimulatorBackend, make_hdd, make_nvme,
                            make_sata_ssd)
from ..models import SLOT_LAYOUT
from ..station import Station

MODE = os.environ.get("DRIVESTATION_MODE", "sim")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="drive-station")

if MODE == "real":
    from ..hw.linux import LinuxBackend
    backend = LinuxBackend()
else:
    backend = SimulatorBackend([s for s, _ in SLOT_LAYOUT])

joblog = JobLog(os.environ.get("DRIVESTATION_DB", "drivestation.db"))
station = Station(backend, joblog)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state() -> dict:
    return {"sim_mode": MODE != "real", "slots": station.snapshot()}


@app.post("/api/slots/{slot_id}/wipe")
def wipe(slot_id: str) -> dict:
    try:
        station.confirm_wipe(slot_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.post("/api/slots/{slot_id}/decline")
def decline(slot_id: str) -> dict:
    station.decline_wipe(slot_id)
    return {"ok": True}


@app.get("/api/records")
def records(serial: Optional[str] = None) -> dict:
    rows = joblog.by_serial(serial) if serial else joblog.recent()
    return {"records": rows}


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
    kind = {"NVME": "NVMe SSD", "SATA_SSD": "SATA SSD",
            "SATA_HDD": "SATA HDD"}.get(r["drive_type"], "drive")
    lines = [f"{r['manufacturer']} {r['model']} {capacity} {kind}"]
    if r["health_percent"] is not None:
        lines.append(f"Health: {r['health_percent']}% remaining")
    lines.append(f"SMART/health check: {r['health_verdict']}")
    if r["result"] == "PASSED":
        date = (r["wipe_finished_at"] or "")[:10]
        lines.append(f"Securely erased ({r['wipe_method'].replace('_', ' ').title()}) "
                     f"on {date} and verified")
    else:
        lines.append(f"WIPE RESULT: {r['result']} — not cleared for resale")
    lines.append(f"Serial: {r['serial']} (traceable wipe record on file)")
    return "\n".join(lines)


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
