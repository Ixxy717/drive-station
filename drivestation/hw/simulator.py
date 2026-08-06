from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models import DriveInfo, DriveType, WipeMethod
from .base import (DriveDisconnected, HardwareBackend, IdentityError,
                   ProgressCallback, VerifyError, WipeError)


@dataclass
class SimFaults:
    """Injectable misbehavior for a fake drive."""
    disconnect_at: Optional[float] = None      # yank at this wipe progress fraction
    second_read_serial: Optional[str] = None   # serial changes after first identity read
    identify_fails: bool = False               # drive won't identify at all
    wipe_rejected: bool = False                # bridge/drive rejects the wipe command
    verify_fails: bool = False                 # verification finds leftover data


@dataclass
class SimDrive:
    info: DriveInfo
    health_raw: dict = field(default_factory=dict)
    faults: SimFaults = field(default_factory=SimFaults)
    identity_reads: int = 0
    # Pre-wipe used bytes for the board ("62 / 256 GB used")
    used_bytes: Optional[int] = 48_000_000_000


class SimulatorBackend(HardwareBackend):
    """Fake docks. Drives are inserted/removed programmatically (tests) or
    via the /api/sim endpoints (dev UI)."""

    def __init__(self, slot_ids: list[str],
                 wipe_duration: float = 6.0,
                 verify_duration: float = 1.5):
        self._slots: dict[str, Optional[SimDrive]] = {s: None for s in slot_ids}
        self._lock = threading.RLock()
        self._on_insert: Callable[[str], None] = lambda s: None
        self._on_remove: Callable[[str], None] = lambda s: None
        self.wipe_duration = wipe_duration
        self.verify_duration = verify_duration

    # -- simulation controls -------------------------------------------------

    def insert_drive(self, slot_id: str, drive: SimDrive) -> None:
        with self._lock:
            self._slots[slot_id] = drive
        self._on_insert(slot_id)

    def remove_drive(self, slot_id: str) -> None:
        with self._lock:
            self._slots[slot_id] = None
        self._on_remove(slot_id)

    def has_drive(self, slot_id: str) -> bool:
        with self._lock:
            return self._slots.get(slot_id) is not None

    # -- HardwareBackend -----------------------------------------------------

    def start(self, on_insert, on_remove) -> None:
        self._on_insert = on_insert
        self._on_remove = on_remove

    def _drive(self, slot_id: str) -> SimDrive:
        with self._lock:
            drive = self._slots.get(slot_id)
        if drive is None:
            raise DriveDisconnected(f"No drive present in {slot_id}")
        return drive

    def read_identity(self, slot_id: str) -> Optional[DriveInfo]:
        drive = self._drive(slot_id)
        if drive.faults.identify_fails:
            return None
        drive.identity_reads += 1
        if drive.faults.second_read_serial and drive.identity_reads > 1:
            info = drive.info
            return DriveInfo(info.manufacturer, info.model,
                             drive.faults.second_read_serial,
                             info.capacity_bytes, info.drive_type)
        return drive.info

    def read_health(self, slot_id: str) -> dict:
        return dict(self._drive(slot_id).health_raw)

    def read_usage(self, slot_id: str) -> dict:
        drive = self._drive(slot_id)
        cap = drive.info.capacity_bytes
        used = drive.used_bytes
        if used is None:
            return {
                "capacity_bytes": cap,
                "used_bytes": None,
                "has_partitions": True,
                "label": f"Data present / {drive.info.capacity_label}",
                "detail": "Simulated unreadable filesystem",
            }
        gb_u = used / 1_000_000_000
        used_lbl = f"{gb_u:.0f}GB" if gb_u >= 10 else f"{gb_u:.1f}GB"
        return {
            "capacity_bytes": cap,
            "used_bytes": used,
            "has_partitions": used > 0,
            "label": f"{used_lbl} / {drive.info.capacity_label} used",
            "detail": "Simulated filesystem usage",
        }

    def supported_wipe_methods(self, slot_id: str) -> list[WipeMethod]:
        drive = self._drive(slot_id)
        if drive.info.drive_type == DriveType.NVME:
            return [WipeMethod.NVME_SANITIZE_CRYPTO,
                    WipeMethod.NVME_SANITIZE_BLOCK,
                    WipeMethod.NVME_FORMAT_SECURE,
                    WipeMethod.ZERO_OVERWRITE]
        if drive.info.drive_type == DriveType.SATA_SSD:
            return [WipeMethod.ATA_SECURE_ERASE_ENHANCED,
                    WipeMethod.ATA_SECURE_ERASE,
                    WipeMethod.ZERO_OVERWRITE]
        return [WipeMethod.ZERO_OVERWRITE]

    def wipe(self, slot_id: str, method: WipeMethod,
             progress: ProgressCallback) -> None:
        drive = self._drive(slot_id)
        if drive.faults.wipe_rejected:
            raise WipeError(f"{method.value} rejected by device/bridge")
        steps = 50
        for i in range(1, steps + 1):
            frac = i / steps
            faults = self._drive(slot_id).faults  # raises if yanked externally
            if faults.disconnect_at is not None and frac >= faults.disconnect_at:
                self.remove_drive(slot_id)
                raise DriveDisconnected(f"Drive disconnected during wipe ({slot_id})")
            time.sleep(self.wipe_duration / steps)
            progress(frac)

    def verify(self, slot_id: str, method: WipeMethod,
               progress: ProgressCallback) -> None:
        steps = 10
        for i in range(1, steps + 1):
            drive = self._drive(slot_id)  # raises if yanked
            time.sleep(self.verify_duration / steps)
            progress(i / steps)
            if drive.faults.verify_fails and i == steps:
                raise VerifyError("Verification found non-erased data")


# -- fake drive presets used by tests and the simulator UI panel -------------

_COUNTER = threading.Lock()
_next_serial = [1000]


def _serial(prefix: str) -> str:
    with _COUNTER:
        _next_serial[0] += 1
        return f"{prefix}{_next_serial[0]}"


def make_nvme(percentage_used: int = 6, media_errors: int = 0,
              critical_warning: int = 0, serial: Optional[str] = None,
              faults: Optional[SimFaults] = None,
              capacity_bytes: int = 512_000_000_000) -> SimDrive:
    # Default under 1TB so grading-dock WIPE runs locally; large-NVMe queue
    # tests pass capacity_bytes >= 1_000_000_000_000 explicitly.
    return SimDrive(
        info=DriveInfo("Samsung", "PM9A1", serial or _serial("SIMNV"),
                       capacity_bytes, DriveType.NVME),
        health_raw={"percentage_used": percentage_used,
                    "media_errors": media_errors,
                    "critical_warning": critical_warning},
        faults=faults or SimFaults(),
    )


def make_sata_ssd(percent_life: Optional[int] = 92, smart_passed: bool = True,
                  serial: Optional[str] = None,
                  faults: Optional[SimFaults] = None) -> SimDrive:
    return SimDrive(
        info=DriveInfo("Samsung", "870 EVO", serial or _serial("SIMSS"),
                       1_000_000_000_000, DriveType.SATA_SSD),
        health_raw={"smart_passed": smart_passed, "percent_life": percent_life},
        faults=faults or SimFaults(),
    )


def make_hdd(reallocated: int = 0, pending: int = 0, uncorrectable: int = 0,
             smart_passed: bool = True, serial: Optional[str] = None,
             faults: Optional[SimFaults] = None) -> SimDrive:
    return SimDrive(
        info=DriveInfo("Western Digital", "WD20EZRZ", serial or _serial("SIMHD"),
                       2_000_000_000_000, DriveType.SATA_HDD),
        health_raw={"smart_passed": smart_passed,
                    "reallocated_sectors": reallocated,
                    "pending_sectors": pending,
                    "uncorrectable_sectors": uncorrectable},
        faults=faults or SimFaults(),
    )
