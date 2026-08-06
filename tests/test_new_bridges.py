"""Tests for the incoming-hardware bridges: ASM2362 NVMe docks (sntasmedia)
and USB SAS enclosures (-d scsi), plus ATA-frozen surfacing."""
import json

from drivestation.health.policy import FROZEN_WARNING, evaluate_health
from drivestation.hw.identify import read_health, read_identity
from drivestation.hw.slots_config import SlotConfig
from drivestation.models import DriveInfo, DriveType, HealthVerdict, WipeMethod
from drivestation.wipe.methods import choose_method


def _asm() -> SlotConfig:
    return SlotConfig("NVME-A1", "path", "asm2362", hot_swap=True)


def _sas() -> SlotConfig:
    return SlotConfig("SAS-1", "path", "sas_usb", hot_swap=True)


def _sat() -> SlotConfig:
    return SlotConfig("SATA-1", "path", "asmedia_sata", hot_swap=False)


# -- ASM2362 (sntasmedia tunnel) ---------------------------------------------

def test_asm2362_identifies_nvme_via_sntasmedia():
    tunnel_payload = {
        "model_name": "Samsung SSD 970 EVO 500GB",
        "serial_number": "S466NX0M123456",
        "user_capacity": {"bytes": 500107862016},
    }

    def run(argv):
        if "sntasmedia" in argv:
            return 0, json.dumps(tunnel_payload), ""
        return 0, json.dumps({}), ""

    info = read_identity("/dev/sdd", _asm(), run)
    assert info is not None
    assert info.serial == "S466NX0M123456"
    assert info.drive_type == DriveType.NVME
    assert info.capacity_bytes == 500107862016


def test_asm2362_health_uses_sntasmedia_not_sntrealtek():
    log = {"nvme_smart_health_information_log": {
        "percentage_used": 4, "media_errors": 0, "critical_warning": 0,
        "available_spare": 100,
    }}
    seen = []

    def run(argv):
        seen.append(list(argv))
        if "sntasmedia" in argv:
            return 0, json.dumps(log), ""
        return 1, "", ""

    raw = read_health("/dev/sdd", _asm(), DriveType.NVME, run)
    assert raw["percentage_used"] == 4
    assert not any("sntrealtek" in a for argv in seen for a in argv)

    info = DriveInfo("Samsung", "970 EVO", "X", 500107862016, DriveType.NVME)
    result = evaluate_health(info, raw)
    assert result.verdict == HealthVerdict.GOOD
    assert result.percent == 96


# -- SAS enclosure (-d scsi) ---------------------------------------------------

_SAS_IDENTIFY = {
    "device": {"protocol": "SCSI"},
    "model_name": "SEAGATE ST900MM0006",
    "serial_number": "S0N1ABCD",
    "rotation_rate": 10000,
    "user_capacity": {"bytes": 900185481216},
}


def test_sas_identifies_via_scsi():
    def run(argv):
        if "scsi" in argv:
            return 0, json.dumps(_SAS_IDENTIFY), ""
        return 1, "", ""

    info = read_identity("/dev/sde", _sas(), run)
    assert info is not None
    assert info.serial == "S0N1ABCD"
    assert info.drive_type == DriveType.SAS_HDD


def test_sata_drive_in_sas_enclosure_stays_sata():
    sat_payload = {
        "device": {"protocol": "ATA"},
        "model_name": "Samsung SSD 870 EVO",
        "serial_number": "S5Y1NX0R",
        "rotation_rate": "Solid State Device",
        "user_capacity": {"bytes": 1000204886016},
        "ata_version": {"string": "ACS-4"},
    }

    def run(argv):
        if "sat" in argv:
            return 0, json.dumps(sat_payload), ""
        # scsi probe answers too (SAT shim), but carries ATA markers
        return 0, json.dumps(sat_payload), ""

    info = read_identity("/dev/sde", _sas(), run)
    assert info is not None
    assert info.drive_type == DriveType.SATA_SSD


def test_sas_health_parses_scsi_log_pages():
    payload = {
        "smart_status": {"passed": True},
        "scsi_grown_defect_list": 2,
        "scsi_error_counter_log": {
            "read": {"total_uncorrected_errors": 0},
            "write": {"total_uncorrected_errors": 0},
        },
    }

    def run(argv):
        assert "scsi" in argv
        return 0, json.dumps(payload), ""

    raw = read_health("/dev/sde", _sas(), DriveType.SAS_HDD, run)
    assert raw["smart_passed"] is True
    assert raw["grown_defects"] == 2
    assert raw["read_uncorrected"] == 0


def test_sas_hdd_good_with_few_defects():
    info = DriveInfo("Seagate", "ST900MM0006", "X", 900185481216,
                     DriveType.SAS_HDD)
    result = evaluate_health(info, {"smart_passed": True, "grown_defects": 2})
    assert result.verdict == HealthVerdict.GOOD
    assert result.percent == 96


def test_sas_hdd_scraps_on_defects_or_uncorrected():
    info = DriveInfo("Seagate", "ST900MM0006", "X", 900185481216,
                     DriveType.SAS_HDD)
    over = evaluate_health(info, {"smart_passed": True, "grown_defects": 9})
    assert over.verdict == HealthVerdict.SCRAP

    errs = evaluate_health(
        info, {"smart_passed": True, "grown_defects": 0,
               "read_uncorrected": 3})
    assert errs.verdict == HealthVerdict.SCRAP


def test_sas_ssd_wear_grading():
    info = DriveInfo("HGST", "HUSMM8020ASS200", "X", 200_000_000_000,
                     DriveType.SAS_SSD)
    good = evaluate_health(info, {"smart_passed": True, "percentage_used": 10})
    assert good.verdict == HealthVerdict.GOOD
    assert good.percent == 90

    worn = evaluate_health(info, {"smart_passed": True, "percentage_used": 40})
    assert worn.verdict == HealthVerdict.SCRAP


def test_sas_unknown_when_enclosure_blocks_log_pages():
    info = DriveInfo("Seagate", "ST900MM0006", "X", 900185481216,
                     DriveType.SAS_HDD)
    result = evaluate_health(info, {})
    assert result.verdict == HealthVerdict.UNKNOWN


def test_sas_wipe_method_is_overwrite():
    assert choose_method(DriveType.SAS_HDD, [WipeMethod.ZERO_OVERWRITE]) \
        == WipeMethod.ZERO_OVERWRITE
    assert choose_method(DriveType.SAS_SSD, [WipeMethod.ZERO_OVERWRITE]) \
        == WipeMethod.ZERO_OVERWRITE


# -- ATA frozen surfaced as a warning ------------------------------------------

def test_frozen_sata_drive_warns_but_stays_good():
    hdparm_text = """
Security:
                frozen
        supported: enhanced erase
"""
    smart_payload = {
        "smart_status": {"passed": True},
        "ata_smart_attributes": {"table": [
            {"name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
            {"name": "Current_Pending_Sector", "raw": {"value": 0}},
            {"name": "Offline_Uncorrectable", "raw": {"value": 0}},
        ]},
    }

    def run(argv):
        if argv[0] == "hdparm":
            return 0, hdparm_text, ""
        return 0, json.dumps(smart_payload), ""

    raw = read_health("/dev/sdc", _sat(), DriveType.SATA_HDD, run)
    assert raw["ata_frozen"] is True

    info = DriveInfo("Western Digital", "WD20EZRZ", "X", 2_000_000_000_000,
                     DriveType.SATA_HDD)
    result = evaluate_health(info, raw)
    assert result.verdict == HealthVerdict.GOOD
    assert FROZEN_WARNING in result.warnings
