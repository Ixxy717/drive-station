import json

from drivestation.health.policy import evaluate_health
from drivestation.hw.identify import ata_security_state, read_health, read_identity
from drivestation.hw.slots_config import SlotConfig
from drivestation.models import DriveType, HealthVerdict


def _sat(bridge="asmedia_sata") -> SlotConfig:
    return SlotConfig("SATA-1", "path", bridge, hot_swap=False)


def _rtl() -> SlotConfig:
    return SlotConfig("NVME-A1", "path", "rtl9210", hot_swap=True)


def test_read_identity_sata_ssd():
    payload = {
        "model_name": "APPLE SSD SM256E",
        "serial_number": "S0X7NZAC700749",
        "rotation_rate": "Solid State Device",
        "user_capacity": {"bytes": 251000193024},
        "device": {"protocol": "ATA"},
    }

    def run(argv):
        if "smartctl" in argv[0] or argv[0] == "smartctl":
            return 0, json.dumps(payload), ""
        return 1, "", "no"

    info = read_identity("/dev/sdc", _sat(), run)
    assert info is not None
    assert info.serial == "S0X7NZAC700749"
    assert info.drive_type == DriveType.SATA_SSD
    assert info.capacity_bytes == 251000193024


def test_rejects_bridge_fake_serial():
    payload = {
        "model_name": "RTL9210C NVME",
        "serial_number": "012345678926",
        "user_capacity": {"bytes": 1000},
    }

    def run(argv):
        return 0, json.dumps(payload), ""

    assert read_identity("/dev/sdd", _rtl(), run) is None


def test_rtl9210_classified_nvme():
    payload = {
        "model_name": "WDC PC SN720 SDAPNTW-256G-1006",
        "serial_number": "19330F807243",
        "user_capacity": {"bytes": 256060514304},
    }

    def run(argv):
        return 0, json.dumps(payload), ""

    info = read_identity("/dev/sdd", _rtl(), run)
    assert info is not None
    assert info.drive_type == DriveType.NVME


def test_ata_security_not_frozen_enhanced():
    text = """
Security:
        Master password revision code = 65534
                not     frozen
        supported: enhanced erase
        6min for SECURITY ERASE UNIT.
"""

    def run(argv):
        return 0, text, ""

    st = ata_security_state("/dev/sdc", run)
    assert st["frozen"] is False
    assert st["enhanced_erase"] is True


def test_ata_security_frozen():
    text = """
Security:
                frozen
        supported: enhanced erase
"""

    def run(argv):
        return 0, text, ""

    st = ata_security_state("/dev/sdc", run)
    assert st["frozen"] is True


def test_nvme_health_unavailable_through_bridge():
    from drivestation.models import DriveInfo

    def run(argv):
        return 0, json.dumps({"smart_status": {"passed": True}}), ""

    raw = read_health("/dev/sdd", _rtl(), DriveType.NVME, run)
    info = DriveInfo("WD", "SN720", "X", 100, DriveType.NVME)
    result = evaluate_health(info, raw)
    assert result.verdict == HealthVerdict.UNKNOWN
    assert result.percent is None
    assert any("unknown" in w.lower() or "blocks" in w.lower() for w in result.warnings)


def test_capacity_falls_back_to_lsblk_when_smart_omits_it():
    payload = {
        "model_name": "WDC PC SN720",
        "serial_number": "19330F807243",
        # no user_capacity — RTL9210 often omits this
    }

    def run(argv):
        if argv[0] == "lsblk":
            return 0, "256060514304\n", ""
        return 0, json.dumps(payload), ""

    info = read_identity("/dev/sdd", _rtl(), run)
    assert info is not None
    assert info.capacity_bytes == 256060514304


def test_hdd_health_attributes():
    payload = {
        "smart_status": {"passed": True},
        "ata_smart_attributes": {
            "table": [
                {"name": "Reallocated_Sector_Ct", "raw": {"value": 8}},
                {"name": "Current_Pending_Sector", "raw": {"value": 0}},
                {"name": "Offline_Uncorrectable", "raw": {"value": 0}},
            ]
        },
    }

    def run(argv):
        return 0, json.dumps(payload), ""

    raw = read_health("/dev/sdc", _sat(), DriveType.SATA_HDD, run)
    assert raw["reallocated_sectors"] == 8
    assert raw["smart_passed"] is True
