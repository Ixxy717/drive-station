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
    wipe_only: bool = False
    # Serial was queued on a grading dock (e.g. StarTech) for wipe here.
    queued_from: Optional[str] = None

    def _health_dict(self) -> dict:
        # Late import — policy imports DriveInfo from this module.
        from .health.policy import health_details
        assert self.health is not None
        return {
            "verdict": self.health.verdict.value,
            "percent": self.health.percent,
            "warnings": self.health.warnings,
            "details": health_details(self.health.raw),
        }

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
                "capacity_bytes": self.drive.capacity_bytes,
                "drive_type": self.drive.drive_type.value,
            },
            "health": None if self.health is None else self._health_dict(),
            "usage": None if self.usage is None else self.usage.to_dict(),
            "progress": round(self.progress, 3),
            "message": self.message,
            "awaiting_confirm": self.awaiting_confirm,
            "wipe_method": self.wipe_method.value if self.wipe_method else None,
            "wipe_only": self.wipe_only,
            "queued_from": self.queued_from,
        }


# SUITOK dual docks — grade elsewhere (StarTech); these are wipe bays for
# large NVMe (and any serial queued from a grading dock).
WIPE_ONLY_SLOTS: frozenset[str] = frozenset({
    "NVME-C1", "NVME-C2", "NVME-D1", "NVME-D2",
})

# NVMe at or above this size should be queued to a wipe-only dock instead of
# zero-overwriting on the StarTech grading toaster.
LARGE_NVME_QUEUE_BYTES = 1_000_000_000_000  # 1 TB marketing


# Permanent physical slot identities. On the real station these map to USB
# port paths via config/slots.toml. Remap after any cable move:
#   sudo tools/dock_characterize.sh --quad   # SATA-1..4
#   sudo tools/dock_characterize.sh --dual   # SATA-5/6
#   sudo tools/dock_characterize.sh          # NVMe + M2 one at a time
SLOT_LAYOUT: list[tuple[str, str]] = [
    # StarTech SDOCK4U313 4-bay — primary SATA (hot-swap, SMART).
    ("SATA-1", "STARTECH 4BAY"),
    ("SATA-2", "STARTECH 4BAY"),
    ("SATA-3", "STARTECH 4BAY"),
    ("SATA-4", "STARTECH 4BAY"),
    # Older 2-bay SATA dock — not hot-swap.
    ("SATA-5", "SATA DOCK"),
    ("SATA-6", "SATA DOCK"),
    # StarTech single-bay NVMe toasters — primary NVMe (real SMART / wear %).
    ("NVME-A1", "NVME GRADE A"),
    ("NVME-B1", "NVME GRADE B"),
    # SUITOK duals — wipe-only (large NVMe / queued from StarTech).
    ("NVME-C1", "WIPE ONLY"),
    ("NVME-C2", "WIPE ONLY"),
    ("NVME-D1", "WIPE ONLY"),
    ("NVME-D2", "WIPE ONLY"),
    # Dual M.2 dock — only bay 1 is reliable alone.
    ("M2-1", "M.2 DOCK"),
]
