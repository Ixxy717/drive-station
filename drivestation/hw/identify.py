"""Drive identity and health via smartctl (and hdparm for ATA security flags)."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..models import DriveInfo, DriveType
from .slots_config import SlotConfig
from .sysfs import RunCmd, default_run_cmd


def _smartctl_json(dev_path: str, extra: list[str], run: RunCmd) -> Optional[dict]:
    argv = ["smartctl", "-j", *extra, dev_path]
    code, out, _ = run(argv)
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    # smartctl returns non-zero for many non-fatal conditions; accept JSON anyway.
    return data


def _pick_identify(dev_path: str, bridge: str, run: RunCmd) -> dict:
    """Try sat first; for Realtek NVMe bridges also try sntrealtek."""
    data = _smartctl_json(dev_path, ["-i", "-d", "sat"], run)
    if data and (data.get("model_name") or data.get("serial_number")
                 or data.get("scsi_model") or (data.get("ata_smart_data"))):
        return data
    # Plain -i without -d
    data = _smartctl_json(dev_path, ["-i"], run) or {}
    if bridge in ("rtl9210", "rtl9220"):
        nvme = _smartctl_json(dev_path, ["-i", "-d", "sntrealtek"], run)
        if nvme and (nvme.get("model_name") or nvme.get("serial_number")
                     or nvme.get("nvme_smart_health_information_log")):
            # Merge: prefer NVMe health later; keep ATA identity if present.
            merged = dict(data)
            merged["_sntrealtek"] = nvme
            for key in ("model_name", "serial_number", "user_capacity"):
                if not merged.get(key) and nvme.get(key):
                    merged[key] = nvme[key]
            return merged
    return data


def _manufacturer_model(model: str) -> tuple[str, str]:
    model = (model or "").strip()
    if not model:
        return "Unknown", "Unknown"
    # Common "VENDOR REST" patterns
    parts = model.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in {
        "WDC", "WD", "SAMSUNG", "APPLE", "SANDISK", "SEAGATE", "TOSHIBA",
        "INTEL", "KINGSTON", "CRUCIAL", "MICRON", "SKHYNIX", "HYNIX",
    }:
        vendor = parts[0]
        if vendor.upper() == "WDC":
            vendor = "Western Digital"
        elif vendor.upper() == "WD":
            vendor = "Western Digital"
        else:
            vendor = vendor.title() if vendor.isupper() else vendor
        return vendor, parts[1]
    return "Unknown", model


def _classify_drive_type(data: dict, slot: SlotConfig) -> DriveType:
    # Explicit NVMe identify via sntrealtek
    if data.get("_sntrealtek") or data.get("device", {}).get("protocol") == "NVMe":
        if slot.bridge in ("rtl9210", "rtl9220"):
            return DriveType.NVME
    protocol = (data.get("device") or {}).get("protocol", "")
    if str(protocol).upper() == "NVME":
        return DriveType.NVME

    model = (data.get("model_name") or "").upper()
    # RTL9210 always hosts NVMe sticks (presented as SCSI)
    if slot.bridge == "rtl9210":
        return DriveType.NVME

    rotation = data.get("rotation_rate")
    if rotation == "Solid State Device" or rotation == 0 or rotation == "0":
        return DriveType.SATA_SSD
    if isinstance(rotation, int) and rotation > 0:
        return DriveType.SATA_HDD
    # Heuristic from model
    if any(x in model for x in ("SSD", "NVME", "NVME", "SN7", "SN5", "PM9", "980", "970")):
        if "NVME" in model or re.search(r"\bSN\d", model):
            if slot.bridge == "rtl9220":
                # Could be NVMe in M.2 dock
                return DriveType.NVME
        return DriveType.SATA_SSD
    if any(x in model for x in ("HDD", "WD20", "WD10", "EZRZ", "EZAZ")):
        return DriveType.SATA_HDD
    if re.search(r"\bST\d{3,}", model):
        return DriveType.SATA_HDD
    # Default: SSD for unknown on these docks (safer health path)
    return DriveType.SATA_SSD


def _capacity_bytes(data: dict) -> int:
    uc = data.get("user_capacity") or {}
    if isinstance(uc, dict) and "bytes" in uc:
        try:
            return int(uc["bytes"])
        except (TypeError, ValueError):
            pass
    # nvme style
    for key in ("nvme_total_capacity", "total_nvm_capacity"):
        if key in data:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                pass
    return 0


def read_identity(
    dev_path: str,
    slot: SlotConfig,
    run: RunCmd = default_run_cmd,
) -> Optional[DriveInfo]:
    data = _pick_identify(dev_path, slot.bridge, run)
    model = (data.get("model_name") or data.get("scsi_model") or "").strip()
    serial = (data.get("serial_number") or data.get("scsi_serial_number") or "").strip()
    # Reject bridge fake serials
    if not serial or re.fullmatch(r"0+", serial) or serial.startswith("0123456789"):
        # try nested
        nested = data.get("_sntrealtek") or {}
        serial = (nested.get("serial_number") or "").strip()
        if not model:
            model = (nested.get("model_name") or "").strip()
    if not serial or not model:
        return None
    if serial.startswith("0123456789"):
        return None

    manufacturer, model_name = _manufacturer_model(model)
    dtype = _classify_drive_type(data, slot)
    # rtl9220 with Solid State + SATA protocol → SATA_SSD
    if slot.bridge == "rtl9220" and dtype == DriveType.NVME:
        proto = str((data.get("device") or {}).get("protocol", "")).upper()
        if proto == "ATA" or data.get("ata_version") or data.get("sata_version"):
            dtype = DriveType.SATA_SSD

    cap = _capacity_bytes(data)
    return DriveInfo(
        manufacturer=manufacturer,
        model=model_name if manufacturer != "Unknown" else model,
        serial=serial,
        capacity_bytes=cap,
        drive_type=dtype,
    )


def _attr_raw(table: list, name_substr: str) -> Optional[int]:
    name_substr = name_substr.lower()
    for row in table or []:
        n = str(row.get("name") or row.get("attribute_name") or "").lower()
        if name_substr in n:
            raw = row.get("raw")
            if isinstance(raw, dict):
                val = raw.get("value")
            else:
                val = raw
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    return None


def read_health(
    dev_path: str,
    slot: SlotConfig,
    drive_type: DriveType,
    run: RunCmd = default_run_cmd,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}

    if drive_type == DriveType.NVME or slot.bridge == "rtl9210":
        nvme = _smartctl_json(dev_path, ["-A", "-H", "-d", "sntrealtek"], run)
        log = {}
        if nvme:
            log = nvme.get("nvme_smart_health_information_log") or {}
        if log:
            if "percentage_used" in log:
                raw["percentage_used"] = int(log["percentage_used"])
            if "media_errors" in log:
                raw["media_errors"] = int(log["media_errors"])
            cw = log.get("critical_warning")
            if cw is not None:
                raw["critical_warning"] = int(cw) if not isinstance(cw, int) else cw
            return raw
        # Fallback: no NVMe SMART through bridge
        return raw

    health = _smartctl_json(dev_path, ["-A", "-H", "-d", "sat"], run) \
        or _smartctl_json(dev_path, ["-A", "-H"], run) or {}
    passed = health.get("smart_status", {}).get("passed")
    if passed is not None:
        raw["smart_passed"] = bool(passed)

    table = (health.get("ata_smart_attributes") or {}).get("table") or []
    if drive_type == DriveType.SATA_HDD:
        raw["reallocated_sectors"] = _attr_raw(table, "reallocated_sector") or 0
        raw["pending_sectors"] = _attr_raw(table, "current_pending") or 0
        raw["uncorrectable_sectors"] = (
            _attr_raw(table, "offline_uncorrectable")
            or _attr_raw(table, "uncorrectable")
            or 0
        )
        return raw

    # SATA SSD wear — vendor specific; best effort
    for key in ("percent_lifetime_remain", "wear_leveling_count",
                "ssd_life_left", "media_wearout"):
        # smartctl JSON sometimes has normalized value on the attr
        for row in table:
            n = str(row.get("name") or "").lower()
            if key.replace("_", "") in n.replace("_", "") or key.split("_")[0] in n:
                val = row.get("value")
                if val is not None:
                    try:
                        raw["percent_life"] = int(val)
                        return raw
                    except (TypeError, ValueError):
                        pass
    # Remaining life attributes often use high normalized value = healthier
    for row in table:
        n = str(row.get("name") or "").lower()
        if "wear" in n or "life" in n or "media_wear" in n:
            val = row.get("value")
            if val is not None:
                try:
                    raw["percent_life"] = int(val)
                    break
                except (TypeError, ValueError):
                    pass
    return raw


def ata_security_state(dev_path: str, run: RunCmd = default_run_cmd) -> dict[str, bool]:
    """Parse hdparm -I security section."""
    code, out, err = run(["hdparm", "-I", dev_path])
    text = out + "\n" + err
    frozen = bool(re.search(r"^\s*frozen\b", text, re.M | re.I)) \
        and not bool(re.search(r"^\s*not\s+frozen\b", text, re.M | re.I))
    # Prefer explicit "not frozen"
    if re.search(r"not\s+frozen", text, re.I):
        frozen = False
    enhanced = bool(re.search(r"supported:\s*enhanced erase", text, re.I))
    enabled = bool(re.search(r"^\s*enabled\b", text, re.M | re.I)) and \
        "Security:" in text
    # "enabled" under Security can mean security feature enabled — careful.
    sec_enabled = bool(re.search(
        r"Security:[\s\S]*?^\s*enabled", text, re.M | re.I))
    return {
        "frozen": frozen,
        "enhanced_erase": enhanced,
        "security_enabled": sec_enabled,
    }
