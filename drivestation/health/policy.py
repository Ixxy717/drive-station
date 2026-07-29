"""Health scoring policy.

IMPORTANT: the numeric thresholds below are PLACEHOLDERS, not finalized
business rules. They exist so the pipeline works end to end; tune them once
real drives have been processed. Everything reads from THRESHOLDS so there is
exactly one place to change.
"""
from __future__ import annotations

from ..models import DriveInfo, DriveType, HealthResult, HealthVerdict

THRESHOLDS = {
    # SSD life remaining (percent) — below these values:
    "ssd_warning_below": 30,
    "ssd_fail_below": 10,
    # HDD sector counts:
    "hdd_realloc_warning_at": 1,
    "hdd_realloc_fail_at": 50,
    "hdd_pending_fail_at": 1,
    "hdd_uncorrectable_fail_at": 1,
    # HDD score deduction per reallocated sector (for the display percentage):
    "hdd_realloc_penalty": 2,
}


def evaluate_health(info: DriveInfo, raw: dict) -> HealthResult:
    if info.drive_type == DriveType.NVME:
        return _evaluate_nvme(raw)
    if info.drive_type == DriveType.SATA_SSD:
        return _evaluate_sata_ssd(raw)
    if info.drive_type == DriveType.SATA_HDD:
        return _evaluate_hdd(raw)
    return HealthResult(HealthVerdict.UNKNOWN, warnings=["Unknown drive type"], raw=raw)


def _ssd_verdict(percent: int, warnings: list[str]) -> HealthVerdict:
    if percent < THRESHOLDS["ssd_fail_below"]:
        warnings.append("Excessive wear")
        return HealthVerdict.FAIL
    if percent < THRESHOLDS["ssd_warning_below"]:
        warnings.append("High wear")
        return HealthVerdict.WARNING
    return HealthVerdict.GOOD


def _evaluate_nvme(raw: dict) -> HealthResult:
    warnings: list[str] = []
    if "percentage_used" not in raw:
        # Common through USB NVMe bridges that block SMART log pages.
        if raw.get("media_errors", 0) > 0:
            return HealthResult(
                HealthVerdict.FAIL, percent=None,
                warnings=[f"Media errors: {raw['media_errors']}"], raw=raw)
        if raw.get("critical_warning", 0) != 0:
            return HealthResult(
                HealthVerdict.FAIL, percent=None,
                warnings=["NVMe critical warning flag set"], raw=raw)
        return HealthResult(
            HealthVerdict.GOOD, percent=None,
            warnings=["Wear level unavailable through USB bridge"], raw=raw)

    percent = max(0, min(100, 100 - int(raw.get("percentage_used", 0))))
    verdict = _ssd_verdict(percent, warnings)
    if raw.get("media_errors", 0) > 0:
        warnings.append(f"Media errors: {raw['media_errors']}")
        verdict = HealthVerdict.FAIL
    if raw.get("critical_warning", 0) != 0:
        warnings.append("NVMe critical warning flag set")
        verdict = HealthVerdict.FAIL
    return HealthResult(verdict, percent=percent, warnings=warnings, raw=raw)


def _evaluate_sata_ssd(raw: dict) -> HealthResult:
    warnings: list[str] = []
    if raw.get("smart_passed") is False:
        return HealthResult(HealthVerdict.FAIL, warnings=["SMART failure"], raw=raw)
    percent = raw.get("percent_life")
    if percent is None:
        # Vendor does not expose a usable wear attribute; SMART pass alone.
        return HealthResult(HealthVerdict.GOOD, percent=None,
                            warnings=["Wear level unavailable for this model"],
                            raw=raw)
    percent = max(0, min(100, int(percent)))
    verdict = _ssd_verdict(percent, warnings)
    return HealthResult(verdict, percent=percent, warnings=warnings, raw=raw)


def _evaluate_hdd(raw: dict) -> HealthResult:
    warnings: list[str] = []
    if raw.get("smart_passed") is False:
        return HealthResult(HealthVerdict.FAIL, warnings=["SMART failure"], raw=raw)

    realloc = int(raw.get("reallocated_sectors", 0))
    pending = int(raw.get("pending_sectors", 0))
    uncorrectable = int(raw.get("uncorrectable_sectors", 0))

    verdict = HealthVerdict.GOOD
    if pending >= THRESHOLDS["hdd_pending_fail_at"]:
        warnings.append(f"Pending sectors: {pending}")
        verdict = HealthVerdict.FAIL
    if uncorrectable >= THRESHOLDS["hdd_uncorrectable_fail_at"]:
        warnings.append(f"Uncorrectable sectors: {uncorrectable}")
        verdict = HealthVerdict.FAIL
    if realloc >= THRESHOLDS["hdd_realloc_fail_at"]:
        warnings.append(f"Reallocated sectors: {realloc}")
        verdict = HealthVerdict.FAIL
    elif realloc >= THRESHOLDS["hdd_realloc_warning_at"]:
        warnings.append(f"Reallocated sectors: {realloc}")
        if verdict == HealthVerdict.GOOD:
            verdict = HealthVerdict.WARNING

    percent = max(0, 100 - realloc * THRESHOLDS["hdd_realloc_penalty"]
                  - (30 if pending else 0) - (30 if uncorrectable else 0))
    return HealthResult(verdict, percent=percent, warnings=warnings, raw=raw)
