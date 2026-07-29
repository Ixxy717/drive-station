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
    for blob in (data, data.get("_sntrealtek") or {}):
        if not isinstance(blob, dict):
            continue
        uc = blob.get("user_capacity") or {}
        if isinstance(uc, dict) and "bytes" in uc:
            try:
                return int(uc["bytes"])
            except (TypeError, ValueError):
                pass
        # nvme / scsi style fields smartctl sometimes uses behind USB bridges
        for key in ("nvme_total_capacity", "total_nvm_capacity",
                    "scsi_capacity", "size"):
            if key in blob and blob[key] is not None:
                try:
                    return int(blob[key])
                except (TypeError, ValueError):
                    pass
        # logical blocks × block size
        lbs = blob.get("logical_block_size") or blob.get("block_size")
        blocks = blob.get("user_capacity_blocks") or blob.get("blocks")
        try:
            if lbs and blocks:
                return int(lbs) * int(blocks)
        except (TypeError, ValueError):
            pass
    return 0


def _lsblk_capacity_bytes(dev_path: str, run: RunCmd) -> int:
    """Kernel-visible size — works when USB bridges omit SMART capacity."""
    code, out, _ = run(["lsblk", "-dbno", "SIZE", dev_path])
    if code != 0 or not out.strip():
        return 0
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def read_identity(
    dev_path: str,
    slot: SlotConfig,
    run: RunCmd = default_run_cmd,
    fallback_capacity_bytes: int = 0,
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
    if cap <= 0 and fallback_capacity_bytes > 0:
        cap = int(fallback_capacity_bytes)
    if cap <= 0:
        cap = _lsblk_capacity_bytes(dev_path, run)
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


def _parse_nvme_health_text(text: str) -> dict[str, Any]:
    """Parse smartctl text when JSON log page is missing (common on RTL9210)."""
    raw: dict[str, Any] = {}
    m = re.search(r"Percentage Used:\s*(\d+)\s*%?", text, re.I)
    if m:
        raw["percentage_used"] = int(m.group(1))
    m = re.search(r"Media and Data Integrity Errors:\s*(\d+)", text, re.I)
    if m:
        raw["media_errors"] = int(m.group(1))
    m = re.search(r"Critical Warning:\s*0x([0-9a-fA-F]+)", text, re.I)
    if m:
        raw["critical_warning"] = int(m.group(1), 16)
    else:
        m = re.search(r"Critical Warning:\s*(\d+)", text, re.I)
        if m:
            raw["critical_warning"] = int(m.group(1))
    m = re.search(r"Available Spare:\s*(\d+)\s*%?", text, re.I)
    if m:
        raw["available_spare"] = int(m.group(1))
    m = re.search(r"Temperature:\s*(\d+)\s*Celsius", text, re.I)
    if m:
        raw["temperature_c"] = int(m.group(1))
    return raw


def _merge_nvme_log(raw: dict[str, Any], log: dict) -> dict[str, Any]:
    if not log:
        return raw
    if "percentage_used" in log:
        raw["percentage_used"] = int(log["percentage_used"])
    if "media_errors" in log:
        raw["media_errors"] = int(log["media_errors"])
    cw = log.get("critical_warning")
    if cw is not None:
        raw["critical_warning"] = int(cw) if not isinstance(cw, int) else cw
    if "available_spare" in log:
        raw["available_spare"] = int(log["available_spare"])
    temp = log.get("temperature")
    if isinstance(temp, int):
        raw["temperature_c"] = temp
    elif isinstance(temp, dict) and "current" in temp:
        try:
            raw["temperature_c"] = int(temp["current"])
        except (TypeError, ValueError):
            pass
    return raw


def _nvme_health_useful(raw: dict[str, Any]) -> bool:
    return any(k in raw for k in (
        "percentage_used", "available_spare", "critical_warning", "media_errors",
    ))


def _read_nvme_health_usb(dev_path: str, run: RunCmd) -> dict[str, Any]:
    """Best-effort NVMe SMART through Realtek USB bridges (sntrealtek tunnel).

    Phase 0 characterization mistakenly ran health WITHOUT ``-d sntrealtek``.
    Identify often works; Get Log Page (wear) needs the tunnel and working
    dock firmware. We try every known smartctl form, then text parse.
    """
    raw: dict[str, Any] = {}
    attempts = (
        ["-a", "-d", "sntrealtek"],
        ["-x", "-d", "sntrealtek"],
        ["-A", "-H", "-d", "sntrealtek"],
        ["-a", "-d", "auto"],
        ["-a", "-d", "sntrealtek,/sat"],
        ["-l", "ssd", "-d", "sntrealtek"],
    )
    for extra in attempts:
        nvme = _smartctl_json(dev_path, extra, run)
        if not nvme:
            continue
        log = nvme.get("nvme_smart_health_information_log") or {}
        raw = _merge_nvme_log(raw, log)
        # SCSI endurance indicator (rare, but cheap to check)
        for key in ("scsi_percentage_used_endurance_indicator",
                    "percentage_used_endurance_indicator"):
            if key in nvme and "percentage_used" not in raw:
                try:
                    raw["percentage_used"] = int(nvme[key])
                except (TypeError, ValueError):
                    pass
        if "percentage_used" in raw:
            return raw
        if _nvme_health_useful(raw):
            # Keep going — prefer percentage_used if a later attempt has it
            continue

    for extra in (
        ["-a", "-d", "sntrealtek"],
        ["-x", "-d", "sntrealtek"],
        ["-a", "-d", "auto"],
        ["-H", "-A", "-d", "sntrealtek"],
    ):
        code, out, err = run(["smartctl", *extra, dev_path])
        text = (out or "") + "\n" + (err or "")
        parsed = _parse_nvme_health_text(text)
        if "percentage_used" in parsed:
            return parsed
        for k, v in parsed.items():
            raw.setdefault(k, v)
        if _nvme_health_useful(raw) and "percentage_used" in raw:
            return raw

    # Raw Realtek tunnel via sg_raw (same CDB smartmontools uses)
    raw.update(_sg_raw_realtek_smart(dev_path, run))
    return raw


def _sg_raw_realtek_smart(dev_path: str, run: RunCmd) -> dict[str, Any]:
    """SCSI CDB 0xE4 NVMe Get Log Page 0x02 — parse SMART bytes ourselves."""
    # CDB: E4 | size_le16=0x0200 | opcode=02 | cdw10_lo=LID=02
    code, out, err = run([
        "sg_raw", "-r", "512", "-b", dev_path,
        "E4", "00", "02", "02", "02",
        "00", "00", "00", "00", "00", "00", "00", "00", "00", "00", "00",
    ])
    if code != 0:
        return {}
    text = (out or "") + "\n" + (err or "")
    # Refuse non-sg_raw chatter (JSON mocks, smartctl dumps, etc.)
    if "smart_status" in text or text.lstrip().startswith("{"):
        return {}
    blob = b""
    if out and ("\x00" in out[:32] or (len(out) >= 512 and "SCSI" not in out[:40])):
        blob = out.encode("latin-1", errors="ignore") if isinstance(out, str) else out
    if len(blob) < 16:
        m = re.search(
            r"Received\s+\d+\s+bytes.*?((?:[0-9a-fA-F]{2}\s+){32,})",
            text, re.S | re.I)
        if not m:
            return {}
        hex_bytes = re.findall(r"[0-9a-fA-F]{2}", m.group(1))
        if len(hex_bytes) < 16:
            return {}
        blob = bytes(int(h, 16) for h in hex_bytes[:512])
    if len(blob) < 16:
        return {}

    # Sanity: available spare is 0-100; temperature Kelvin usually ~280-340
    spare = blob[3]
    kelvin = int.from_bytes(blob[1:3], "little")
    if spare > 100 and not (200 < kelvin < 400):
        return {}

    raw: dict[str, Any] = {
        "critical_warning": blob[0],
        "available_spare": spare,
        "percentage_used": blob[5],
    }
    if len(blob) >= 96:
        raw["media_errors"] = int.from_bytes(blob[80:96], "little")
    if 200 < kelvin < 400:
        raw["temperature_c"] = kelvin - 273
    return raw


def read_health(
    dev_path: str,
    slot: SlotConfig,
    drive_type: DriveType,
    run: RunCmd = default_run_cmd,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}

    if drive_type == DriveType.NVME or slot.bridge == "rtl9210":
        return _read_nvme_health_usb(dev_path, run)

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
