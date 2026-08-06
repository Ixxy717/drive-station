from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SlotStatus(str, Enum):
    EMPTY = "EMPTY"
    DETECTED = "DETECTED"
    CHECKING = "CHECKING"
    READY = "READY"
    WIPING = "WIPING"
    VERIFYING = "VERIFYING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"


class DriveType(str, Enum):
    NVME = "NVME"
    SATA_SSD = "SATA_SSD"
    SATA_HDD = "SATA_HDD"
    SAS_HDD = "SAS_HDD"
    SAS_SSD = "SAS_SSD"
    UNKNOWN = "UNKNOWN"


class HealthVerdict(str, Enum):
    GOOD = "GOOD"
    WARNING = "WARNING"   # soft flag; scrap rules usually promote to SCRAP
    SCRAP = "SCRAP"       # do not resell
    FAIL = "FAIL"         # SMART / media hard failure (also not for resale)
    UNKNOWN = "UNKNOWN"


class WipeMethod(str, Enum):
    NVME_SANITIZE_CRYPTO = "NVME_SANITIZE_CRYPTO"
    NVME_SANITIZE_BLOCK = "NVME_SANITIZE_BLOCK"
    NVME_FORMAT_SECURE = "NVME_FORMAT_SECURE"
    ATA_SECURE_ERASE_ENHANCED = "ATA_SECURE_ERASE_ENHANCED"
    ATA_SECURE_ERASE = "ATA_SECURE_ERASE"
    ZERO_OVERWRITE = "ZERO_OVERWRITE"


@dataclass(frozen=True)
class DriveInfo:
    manufacturer: str
    model: str
    serial: str
    capacity_bytes: int
    drive_type: DriveType

    @property
    def capacity_label(self) -> str:
        gb = self.capacity_bytes / 1_000_000_000
        if gb >= 1000:
            return f"{gb / 1000:.0f}TB" if (gb / 1000) == int(gb / 1000) else f"{gb / 1000:.1f}TB"
        return f"{gb:.0f}GB"


@dataclass
class HealthResult:
    verdict: HealthVerdict
    percent: Optional[int] = None
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class UsageSnapshot:
    """Pre-wipe occupancy shown on the board / stored on the job."""
    capacity_bytes: int
    used_bytes: Optional[int]
    has_partitions: bool
    label: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "has_partitions": self.has_partitions,
            "label": self.label,
            "detail": self.detail,
        }


@dataclass
class SlotState:
    slot_id: str
    group: str
    status: SlotStatus = SlotStatus.EMPTY
    drive: Optional[DriveInfo] = None
    health: Optional[HealthResult] = None
    usage: Optional[UsageSnapshot] = None
    progress: float = 0.0
    message: str = ""
    awaiting_confirm: bool = False
    wipe_method: Optional[WipeMethod] = None

    def to_dict(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "group": self.group,
            "status": self.status.value,
            "drive": None if self.drive is None else {
                "manufacturer": self.drive.manufacturer,
                "model": self.drive.model,
                "serial": self.drive.serial,
                "capacity": self.drive.capacity_label,
                "drive_type": self.drive.drive_type.value,
            },
            "health": None if self.health is None else {
                "verdict": self.health.verdict.value,
                "percent": self.health.percent,
                "warnings": self.health.warnings,
            },
            "usage": None if self.usage is None else self.usage.to_dict(),
            "progress": round(self.progress, 3),
            "message": self.message,
            "awaiting_confirm": self.awaiting_confirm,
            "wipe_method": self.wipe_method.value if self.wipe_method else None,
        }


# Permanent physical slot identities. On the real station these map to USB
# port paths via the allowlist created by the setup wizard (see hw/linux.py).
SLOT_LAYOUT: list[tuple[str, str]] = [
    ("SATA-1", "SATA DOCK"),
    ("SATA-2", "SATA DOCK"),
    ("NVME-A1", "NVME DOCK A"),
    ("NVME-A2", "NVME DOCK A"),
    ("NVME-B1", "NVME DOCK B"),
    ("NVME-B2", "NVME DOCK B"),
    # Single usable bay on the dual M.2 SATA/NVMe dock (bay 2 needs bay 1
    # occupied for transfer — we only expose the reliable bay as M2-1).
    ("M2-1", "M.2 DOCK"),
]
