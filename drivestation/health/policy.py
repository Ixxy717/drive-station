"""Health scoring / scrap policy for resale grading.

Verdicts used on the board:
  GOOD  — fine to wipe and list
  SCRAP — do not resell (still may be wiped for destruction)
  FAIL  — SMART/media hard failure (treated like scrap for resale)
"""
from __future__ import annotations

from ..models import DriveInfo, DriveType, HealthResult, HealthVerdict

THRESHOLDS = {
    # SSD / NVMe life remaining (%). At or below → SCRAP.
    "ssd_scrap_at_or_below": 70,
    # HDD display score. At or below → SCRAP.
    "hdd_scrap_at_or_below": 80,
    # HDD reallocated sectors. Strictly more than this → SCRAP.
    "hdd_realloc_scrap_above": 5,
    # Immediate scrap on these HDD counts:
    "hdd_pending_fail_at": 1,
    "hdd_uncorrectable_fail_at": 1,
    # HDD score deduction per reallocated sector (display % only):
    "hdd_realloc_penalty": 2,
    # SAS grown defect list entries. Strictly more than this → SCRAP.
    "sas_defects_scrap_above": 5,
}

FROZEN_WARNING = "ATA security frozen — replug dock for fast secure erase"


def evaluate_health(info: DriveInfo, raw: dict) -> HealthResult:
    if info.drive_type == DriveType.NVME:
        return _evaluate_nvme(raw)
    if info.drive_type == DriveType.SATA_SSD:
        return _evaluate_sata_ssd(raw)
    if info.drive_type == DriveType.SATA_HDD:
        return _evaluate_hdd(raw)
    if info.drive_type in (DriveType.SAS_HDD, DriveType.SAS_SSD):
        return _evaluate_sas(raw, ssd=info.drive_type == DriveType.SAS_SSD)
    return HealthResult(HealthVerdict.UNKNOWN, warnings=["Unknown drive type"], raw=raw)


def _ssd_life_verdict(percent: int, warnings: list[str]) -> HealthVerdict:
    if percent <= THRESHOLDS["ssd_scrap_at_or_below"]:
        warnings.append("SCRAP — wear at or below 70%")
        return HealthVerdict.SCRAP
    return HealthVerdict.GOOD


def _evaluate_nvme(raw: dict) -> HealthResult:
    warnings: list[str] = []

    if raw.get("media_errors", 0) > 0:
        return HealthResult(
            HealthVerdict.SCRAP, percent=None,
            warnings=[f"SCRAP — media errors: {raw['media_errors']}"], raw=raw)
    if raw.get("critical_warning", 0) != 0:
        return HealthResult(
            HealthVerdict.SCRAP, percent=None,
            warnings=["SCRAP — NVMe critical warning"], raw=raw)

    spare = raw.get("available_spare")
    if spare is not None and int(spare) < 10:
        return HealthResult(
            HealthVerdict.SCRAP, percent=int(spare) if "percentage_used" not in raw else None,
            warnings=[f"SCRAP — available spare {spare}%"], raw=raw)

    if "percentage_used" in raw:
        percent = max(0, min(100, 100 - int(raw["percentage_used"])))
        verdict = _ssd_life_verdict(percent, warnings)
        return HealthResult(verdict, percent=percent, warnings=warnings, raw=raw)

    # Partial health (common when bridge returns log with spare but wear quirks)
    if spare is not None:
        warnings.append(f"Available spare {spare}% (wear % not reported)")
        return HealthResult(
            HealthVerdict.GOOD, percent=None, warnings=warnings, raw=raw)

    return HealthResult(
        HealthVerdict.UNKNOWN, percent=None,
        warnings=["Health unknown — USB bridge blocks NVMe SMART"], raw=raw)


def _evaluate_sas(raw: dict, ssd: bool) -> HealthResult:
    """SAS drives grade on SCSI log pages: grown defects, uncorrected error
    counters, endurance indicator (SSDs)."""
    warnings: list[str] = []

    if raw.get("smart_passed") is False:
        return HealthResult(
            HealthVerdict.SCRAP, warnings=["SCRAP — SMART failure"], raw=raw)

    uncorrected = sum(int(raw.get(k, 0)) for k in
                      ("read_uncorrected", "write_uncorrected",
                       "verify_uncorrected"))
    defects = raw.get("grown_defects")

    scrap_reasons: list[str] = []
    if uncorrected > 0:
        scrap_reasons.append(f"uncorrected errors: {uncorrected}")
    if defects is not None and int(defects) > THRESHOLDS["sas_defects_scrap_above"]:
        scrap_reasons.append(
            f"grown defects: {defects} "
            f"(over {THRESHOLDS['sas_defects_scrap_above']})")

    percent = None
    if ssd and "percentage_used" in raw:
        percent = max(0, min(100, 100 - int(raw["percentage_used"])))
        if percent <= THRESHOLDS["ssd_scrap_at_or_below"]:
            scrap_reasons.append(
                f"wear at or below {THRESHOLDS['ssd_scrap_at_or_below']}%")
    elif not ssd and defects is not None:
        percent = max(0, 100 - int(defects) * THRESHOLDS["hdd_realloc_penalty"]
                      - (30 if uncorrected else 0))
        if percent <= THRESHOLDS["hdd_scrap_at_or_below"]:
            scrap_reasons.append(
                f"health {percent}% (at or below "
                f"{THRESHOLDS['hdd_scrap_at_or_below']}%)")

    if scrap_reasons:
        warnings.append("SCRAP — " + "; ".join(scrap_reasons))
        return HealthResult(HealthVerdict.SCRAP, percent=percent,
                            warnings=warnings, raw=raw)

    # Nothing usable came back — likely the enclosure blocks SCSI log pages.
    if raw.get("smart_passed") is None and defects is None and \
            "percentage_used" not in raw:
        return HealthResult(
            HealthVerdict.UNKNOWN, percent=None,
            warnings=["Health unknown — enclosure returned no SCSI health"],
            raw=raw)

    if defects:
        warnings.append(f"Grown defects: {defects}")
    return HealthResult(HealthVerdict.GOOD, percent=percent,
                        warnings=warnings, raw=raw)


def _evaluate_sata_ssd(raw: dict) -> HealthResult:
    warnings: list[str] = []
    if raw.get("ata_frozen"):
        warnings.append(FROZEN_WARNING)
    if raw.get("smart_passed") is False:
        return HealthResult(
            HealthVerdict.SCRAP, warnings=["SCRAP — SMART failure"], raw=raw)
    percent = raw.get("percent_life")
    if percent is None:
        return HealthResult(
            HealthVerdict.UNKNOWN, percent=None,
            warnings=warnings + ["Health unknown — no wear attribute from this drive"],
            raw=raw)
    percent = max(0, min(100, int(percent)))
    verdict = _ssd_life_verdict(percent, warnings)
    return HealthResult(verdict, percent=percent, warnings=warnings, raw=raw)


def _evaluate_hdd(raw: dict) -> HealthResult:
    warnings: list[str] = []
    if raw.get("ata_frozen"):
        warnings.append(FROZEN_WARNING)
    if raw.get("smart_passed") is False:
        return HealthResult(
            HealthVerdict.SCRAP, warnings=["SCRAP — SMART failure"], raw=raw)

    realloc = int(raw.get("reallocated_sectors", 0))
    pending = int(raw.get("pending_sectors", 0))
    uncorrectable = int(raw.get("uncorrectable_sectors", 0))

    percent = max(0, 100 - realloc * THRESHOLDS["hdd_realloc_penalty"]
                  - (30 if pending else 0) - (30 if uncorrectable else 0))

    scrap_reasons: list[str] = []
    if pending >= THRESHOLDS["hdd_pending_fail_at"]:
        scrap_reasons.append(f"pending sectors: {pending}")
    if uncorrectable >= THRESHOLDS["hdd_uncorrectable_fail_at"]:
        scrap_reasons.append(f"uncorrectable sectors: {uncorrectable}")
    if realloc > THRESHOLDS["hdd_realloc_scrap_above"]:
        scrap_reasons.append(f"reallocated sectors: {realloc} (over 5)")
    if percent <= THRESHOLDS["hdd_scrap_at_or_below"]:
        scrap_reasons.append(f"health {percent}% (at or below 80%)")

    if scrap_reasons:
        warnings.append("SCRAP — " + "; ".join(scrap_reasons))
        return HealthResult(HealthVerdict.SCRAP, percent=percent,
                            warnings=warnings, raw=raw)

    if realloc > 0:
        warnings.append(f"Reallocated sectors: {realloc}")

    return HealthResult(HealthVerdict.GOOD, percent=percent,
                        warnings=warnings, raw=raw)
