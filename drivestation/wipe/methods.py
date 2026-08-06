"""Wipe method selection.

Preference order per drive type. On the real station, the backend reports
which methods actually pass through the specific dock's USB bridge (learned
in Phase 0 characterization); we pick the most preferred supported one.
"""
from __future__ import annotations

from typing import Optional

from ..models import DriveType, WipeMethod

PREFERENCE: dict[DriveType, list[WipeMethod]] = {
    DriveType.NVME: [
        WipeMethod.NVME_SANITIZE_CRYPTO,
        WipeMethod.NVME_SANITIZE_BLOCK,
        WipeMethod.NVME_FORMAT_SECURE,
        WipeMethod.ZERO_OVERWRITE,
    ],
    DriveType.SATA_SSD: [
        WipeMethod.ATA_SECURE_ERASE_ENHANCED,
        WipeMethod.ATA_SECURE_ERASE,
        WipeMethod.ZERO_OVERWRITE,
    ],
    DriveType.SATA_HDD: [
        WipeMethod.ATA_SECURE_ERASE_ENHANCED,
        WipeMethod.ZERO_OVERWRITE,
    ],
    # SAS: SCSI FORMAT UNIT / sanitize not implemented yet — overwrite only.
    DriveType.SAS_HDD: [WipeMethod.ZERO_OVERWRITE],
    DriveType.SAS_SSD: [WipeMethod.ZERO_OVERWRITE],
    DriveType.UNKNOWN: [WipeMethod.ZERO_OVERWRITE],
}


def choose_method(drive_type: DriveType,
                  supported: list[WipeMethod]) -> Optional[WipeMethod]:
    for method in PREFERENCE[drive_type]:
        if method in supported:
            return method
    return None
