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


# smartctl NVMe-over-USB tunnel device type per bridge chip.
NVME_TUNNEL_BY_BRIDGE = {
    "rtl9210": "sntrealtek",
    "rtl9220": "sntrealtek",
    "asm2362": "sntasmedia",
}


def _identity_present(data: Optional[dict]) -> bool:
    return bool(data and (data.get("model_name") or data.get("serial_number")
                          or data.get("scsi_model")
                          or data.get("ata_smart_data")))


def _pick_identify(dev_path: str, bridge: str, run: RunCmd) -> dict:
    """Try sat first; NVMe bridges get their tunnel; SAS enclosures get scsi."""
    if bridge == "sas_usb":
        # SAS drives never answer sat — go straight to scsi, then sat for
        # SATA drives sitting in the same enclosure.
        data = _smartctl_json(dev_path, ["-i", "-d", "scsi"], run)
        if _identity_present(data) and _is_sas(data):
            return data
        sat = _smartctl_json(dev_path, ["-i", "-d", "sat"], run)
        if _identity_present(sat):
            return sat
        return data if _identity_present(data) else (
            _smartctl_json(dev_path, ["-i"], run) or {})

    # SAT often answers first on USB NVMe bridges with a wrong/bridge serial.
    # Always try the NVMe tunnel for those chips and prefer tunnel identity.
    sat = _smartctl_json(dev_path, ["-i", "-d", "sat"], run)
    plain = _smartctl_json(dev_path, ["-i"], run) or {}
    data = sat if _identity_present(sat) else plain
    tunnel = NVME_TUNNEL_BY_BRIDGE.get(bridge)
    if tunnel:
        nvme = _smartctl_json(dev_path, ["-i", "-d", tunnel], run)
        if nvme and (nvme.get("model_name") or nvme.get("serial_number")
                     or nvme.get("nvme_smart_health_information_log")):
            merged = dict(data) if _identity_present(data) else {}
            merged["_nvme_tunnel"] = nvme
            for key in ("model_name", "serial_number", "user_capacity"):
                if nvme.get(key):
                    merged[key] = nvme[key]
                elif data and data.get(key) and not merged.get(key):
                    merged[key] = data[key]
            return merged
    return data if _identity_present(data) else plain


def _is_sas(data: Optional[dict]) -> bool:
    """True when smartctl talked SCSI to a real SAS target (not a SAT shim)."""
    if not data:
        return False
    protocol = str((data.get("device") or {}).get("protocol", "")).upper()
    if protocol != "SCSI":
        return False
    # SAT-tunneled SATA drives also report SCSI protocol but carry ATA info.
    if data.get("ata_version") or data.get("sata_version"):
        return False
    return True


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
    # SAS enclosure with a real SCSI target
    if slot.bridge == "sas_usb" and _is_sas(data):
        rotation = data.get("rotation_rate")
        if rotation == "Solid State Device" or rotation == 0 or rotation == "0":
            return DriveType.SAS_SSD
        return DriveType.SAS_HDD

    # Explicit NVMe identify via bridge tunnel
    if data.get("_nvme_tunnel") or data.get("device", {}).get("protocol") == "NVMe":
        if slot.bridge in NVME_TUNNEL_BY_BRIDGE:
            return DriveType.NVME
    protocol = (data.get("device") or {}).get("protocol", "")
    if str(protocol).upper() == "NVME":
        return DriveType.NVME

    model = (data.get("model_name") or "").upper()
    # RTL9210 / ASM2362 always host NVMe sticks (presented as SCSI)
    if slot.bridge in ("rtl9210", "asm2362"):
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
    for blob in (data, data.get("_nvme_tunnel") or {}):
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
        nested = data.get("_nvme_tunnel") or {}
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


def _attr_temperature_c(table: list) -> Optional[int]:
    """ATA temp raw is often packed (min/max in high bytes). Prefer string/value."""
    for row in table or []:
        n = str(row.get("name") or "").lower()
        if "temperature" not in n:
            continue
        raw = row.get("raw")
        if isinstance(raw, dict):
            m = re.match(r"(-?\d+)", str(raw.get("string") or ""))
            if m:
                return int(m.group(1))
            val = raw.get("value")
            if isinstance(val, int):
                low = val & 0xFF
                if 0 <= low <= 120:
                    return low
        v = row.get("value")
        if isinstance(v, int) and 0 <= v <= 120:
            return v
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
    m = re.search(r"Power On Hours:\s*(\d+)", text, re.I)
    if m:
        raw["power_on_hours"] = int(m.group(1))
    m = re.search(r"Power Cycles:\s*(\d+)", text, re.I)
    if m:
        raw["power_cycles"] = int(m.group(1))
    m = re.search(r"Unsafe Shutdowns:\s*(\d+)", text, re.I)
    if m:
        raw["unsafe_shutdowns"] = int(m.group(1))
    # "Data Units Written: 1,234,567 [632 GB]" — prefer bracket size if present
    m = re.search(
        r"Data Units Written:\s*([\d,]+)\s*(?:\[([^\]]+)\])?", text, re.I)
    if m:
        raw["data_units_written"] = int(m.group(1).replace(",", ""))
        if m.group(2):
            raw["data_written_label"] = m.group(2).strip()
    m = re.search(
        r"Data Units Read:\s*([\d,]+)\s*(?:\[([^\]]+)\])?", text, re.I)
    if m:
        raw["data_units_read"] = int(m.group(1).replace(",", ""))
        if m.group(2):
            raw["data_read_label"] = m.group(2).strip()
    return raw


_NVME_LOG_INT_KEYS = (
    "percentage_used", "available_spare", "available_spare_threshold",
    "media_errors", "critical_warning", "power_on_hours", "power_cycles",
    "unsafe_shutdowns", "data_units_written", "data_units_read",
    "host_reads", "host_writes", "controller_busy_time",
    "num_err_log_entries",
)


def _merge_nvme_log(raw: dict[str, Any], log: dict) -> dict[str, Any]:
    if not log:
        return raw
    for key in _NVME_LOG_INT_KEYS:
        if key not in log:
            continue
        try:
            raw[key] = int(log[key])
        except (TypeError, ValueError):
            pass
    temp = log.get("temperature")
    if isinstance(temp, int):
        raw["temperature_c"] = temp
    elif isinstance(temp, dict) and "current" in temp:
        try:
            raw["temperature_c"] = int(temp["current"])
        except (TypeError, ValueError):
            pass
    return raw


def _enrich_from_smartctl_json(raw: dict[str, Any], blob: dict) -> dict[str, Any]:
    """Pull NVMe log + top-level power-on time from a smartctl -j object."""
    log = blob.get("nvme_smart_health_information_log") or {}
    raw = _merge_nvme_log(raw, log)
    pot = blob.get("power_on_time")
    if "power_on_hours" not in raw:
        if isinstance(pot, dict) and pot.get("hours") is not None:
            try:
                raw["power_on_hours"] = int(pot["hours"])
            except (TypeError, ValueError):
                pass
        elif isinstance(pot, (int, float)):
            raw["power_on_hours"] = int(pot)
    return raw


def _nvme_health_useful(raw: dict[str, Any]) -> bool:
    return any(k in raw for k in (
        "percentage_used", "available_spare", "critical_warning", "media_errors",
    ))


def _read_nvme_health_usb(
    dev_path: str, run: RunCmd, tunnel: str = "sntrealtek",
) -> dict[str, Any]:
    """Best-effort NVMe SMART through a USB bridge tunnel (sntrealtek or
    sntasmedia).

    Phase 0 characterization mistakenly ran health WITHOUT the tunnel flag.
    Identify often works; Get Log Page (wear) needs the tunnel and working
    dock firmware. We try every known smartctl form, then text parse.
    """
    raw: dict[str, Any] = {}
    attempts = (
        ["-a", "-d", tunnel],
        ["-x", "-d", tunnel],
        ["-A", "-H", "-d", tunnel],
        ["-a", "-d", "auto"],
        ["-a", "-d", f"{tunnel},/sat"],
        ["-l", "ssd", "-d", tunnel],
    )
    for extra in attempts:
        nvme = _smartctl_json(dev_path, extra, run)
        if not nvme:
            continue
        raw = _enrich_from_smartctl_json(raw, nvme)
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
        ["-a", "-d", tunnel],
        ["-x", "-d", tunnel],
        ["-a", "-d", "auto"],
        ["-H", "-A", "-d", tunnel],
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

    # Raw Realtek tunnel via sg_raw (same CDB smartmontools uses).
    # The CDB is Realtek-specific — skip on other bridges.
    if tunnel == "sntrealtek":
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


def _read_sas_health(dev_path: str, run: RunCmd) -> dict[str, Any]:
    """SAS drive health via SCSI log pages (smartctl -d scsi).

    Only sets keys the drive actually reported, so the policy layer can tell
    "healthy" apart from "enclosure returned nothing".
    """
    raw: dict[str, Any] = {}
    health = _smartctl_json(dev_path, ["-A", "-H", "-d", "scsi"], run) or {}

    passed = (health.get("smart_status") or {}).get("passed")
    if passed is not None:
        raw["smart_passed"] = bool(passed)

    defects = health.get("scsi_grown_defect_list")
    if defects is not None:
        try:
            raw["grown_defects"] = int(defects)
        except (TypeError, ValueError):
            pass

    counters = health.get("scsi_error_counter_log") or {}
    for op in ("read", "write", "verify"):
        entry = counters.get(op) or {}
        val = entry.get("total_uncorrected_errors")
        if val is not None:
            try:
                raw[f"{op}_uncorrected"] = int(val)
            except (TypeError, ValueError):
                pass

    # SAS SSD wear (Solid State Media log page)
    for key in ("scsi_percentage_used_endurance_indicator",
                "percentage_used_endurance_indicator"):
        if health.get(key) is not None:
            try:
                raw["percentage_used"] = int(health[key])
                break
            except (TypeError, ValueError):
                pass
    return raw


def read_health(
    dev_path: str,
    slot: SlotConfig,
    drive_type: DriveType,
    run: RunCmd = default_run_cmd,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}

    if drive_type in (DriveType.SAS_HDD, DriveType.SAS_SSD):
        return _read_sas_health(dev_path, run)

    if drive_type == DriveType.NVME or slot.bridge in ("rtl9210", "asm2362"):
        tunnel = NVME_TUNNEL_BY_BRIDGE.get(slot.bridge, "sntrealtek")
        return _read_nvme_health_usb(dev_path, run, tunnel)

    health = _smartctl_json(dev_path, ["-A", "-H", "-d", "sat"], run) \
        or _smartctl_json(dev_path, ["-A", "-H"], run) or {}
    passed = health.get("smart_status", {}).get("passed")
    if passed is not None:
        raw["smart_passed"] = bool(passed)

    # Frozen / locked security changes wipe options (dd still works when
    # unlocked enough to accept I/O; locked needs a password we don't have).
    if drive_type in (DriveType.SATA_SSD, DriveType.SATA_HDD):
        sec = ata_security_state(dev_path, run)
        if sec["frozen"]:
            raw["ata_frozen"] = True
        if sec.get("locked"):
            raw["ata_locked"] = True
        if sec.get("security_enabled"):
            raw["ata_security_enabled"] = True

    table = (health.get("ata_smart_attributes") or {}).get("table") or []

    # Operator telemetry (hours / cycles / written) — always collect when present.
    poh = _attr_raw(table, "power_on_hours")
    if poh is not None:
        raw["power_on_hours"] = poh
    cycles = _attr_raw(table, "power_cycle")
    if cycles is not None:
        raw["power_cycles"] = cycles
    temp = _attr_temperature_c(table)
    if temp is not None:
        raw["temperature_c"] = temp
    lbas_w = (
        _attr_raw(table, "total_lbas_written")
        or _attr_raw(table, "lbas_written")
        or _attr_raw(table, "total_lb_as_written")
    )
    if lbas_w is not None:
        raw["total_lbas_written"] = lbas_w
    lbas_r = (
        _attr_raw(table, "total_lbas_read")
        or _attr_raw(table, "lbas_read")
    )
    if lbas_r is not None:
        raw["total_lbas_read"] = lbas_r

    if drive_type == DriveType.SATA_HDD:
        raw["reallocated_sectors"] = _attr_raw(table, "reallocated_sector") or 0
        raw["pending_sectors"] = _attr_raw(table, "current_pending") or 0
        raw["uncorrectable_sectors"] = (
            _attr_raw(table, "offline_uncorrectable")
            or _attr_raw(table, "uncorrectable")
            or 0
        )
        return raw

    # SATA SSD wear — vendor specific; best effort (do not return early —
    # telemetry above must stay on the board).
    for key in ("percent_lifetime_remain", "wear_leveling_count",
                "ssd_life_left", "media_wearout", "lifetime_remaining"):
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
    # Restrict to the Security: block so other "enabled"/"locked" lines
    # (e.g. feature lists) don't false-positive.
    sec = ""
    m = re.search(r"Security:\s*\n([\s\S]*?)(?:\n[A-Z][\w ]*:|\Z)", text)
    if m:
        sec = m.group(1)
    else:
        sec = text

    frozen = bool(re.search(r"^\s*frozen\b", sec, re.M | re.I)) \
        and not bool(re.search(r"^\s*not\s+frozen\b", sec, re.M | re.I))
    if re.search(r"not\s+frozen", sec, re.I):
        frozen = False
    locked = bool(re.search(r"^\s*locked\b", sec, re.M | re.I)) \
        and not bool(re.search(r"^\s*not\s+locked\b", sec, re.M | re.I))
    if re.search(r"not\s+locked", sec, re.I):
        locked = False
    enhanced = bool(re.search(r"supported:\s*enhanced erase", sec, re.I))
    sec_enabled = bool(re.search(r"^\s*enabled\b", sec, re.M | re.I)) \
        and not bool(re.search(r"^\s*not\s+enabled\b", sec, re.M | re.I))
    if re.search(r"not\s+enabled", sec, re.I):
        sec_enabled = False
    return {
        "frozen": frozen,
        "locked": locked,
        "enhanced_erase": enhanced,
        "security_enabled": sec_enabled,
    }
