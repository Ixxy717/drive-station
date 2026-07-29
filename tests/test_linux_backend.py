"""LinuxBackend presence reconcile + wipe method selection (mocked cmds)."""
from __future__ import annotations

import json

from drivestation.db import JobLog
from drivestation.hw.linux import LinuxBackend
from drivestation.hw.slots_config import load_slots_config
from drivestation.models import SlotStatus, WipeMethod
from drivestation.station import Station

from .conftest import wait_for
from .test_linux_sysfs import _make_run


class MutableDeviceTable:
    def __init__(self, devices: list[dict]):
        self.devices = list(devices)

    def run(self, argv: list[str]):
        return _make_run(self.devices)(argv)


def _smart_identity(model="APPLE SSD SM256E", serial="SER123",
                    rotation="Solid State Device"):
    return {
        "model_name": model,
        "serial_number": serial,
        "rotation_rate": rotation,
        "user_capacity": {"bytes": 251000193024},
        "device": {"protocol": "ATA"},
        "smart_status": {"passed": True},
        "ata_smart_attributes": {"table": []},
    }


def _hdparm_ok():
    return """
Security:
        Master password revision code = 65534
                not     frozen
        supported: enhanced erase
"""


def test_reconcile_insert_remove_and_ignore_rogue(tmp_path):
    slots = load_slots_config()
    table = MutableDeviceTable([])
    events: list[tuple[str, str]] = []

    def run(argv):
        cmd = argv[0]
        if cmd == "smartctl":
            return 0, json.dumps(_smart_identity()), ""
        if cmd == "hdparm":
            return 0, _hdparm_ok(), ""
        return table.run(argv)

    backend = LinuxBackend(
        run_cmd=run, poll_interval=0.05, use_pyudev=False,
    )
    backend.start(
        on_insert=lambda s: events.append(("in", s)),
        on_remove=lambda s: events.append(("out", s)),
    )

    # Insert SATA-1
    table.devices = [{
        "name": "sdc", "size": 251_000_193_024,
        "id_path": slots["SATA-1"].id_path,
    }]
    backend._reconcile()
    assert ("in", "SATA-1") in events
    assert backend.read_identity("SATA-1").serial == "SER123"

    # Ghost (0B) → remove
    table.devices = [{
        "name": "sdc", "size": 0,
        "id_path": slots["SATA-1"].id_path,
    }]
    backend._reconcile()
    assert ("out", "SATA-1") in events

    # Rogue never maps
    events.clear()
    table.devices = [{
        "name": "sdz", "size": 8_000_000_000,
        "id_path": "pci-usb-rogue",
    }]
    backend._reconcile()
    assert events == []
    backend.stop()


def test_nvme_slot_only_offers_overwrite(tmp_path):
    slots = load_slots_config()
    table = MutableDeviceTable([{
        "name": "sdd", "size": 256_060_514_304,
        "id_path": slots["NVME-A1"].id_path,
    }])

    def run(argv):
        if argv[0] == "smartctl":
            return 0, json.dumps({
                "model_name": "WDC PC SN720",
                "serial_number": "NVME123",
                "user_capacity": {"bytes": 256060514304},
            }), ""
        if argv[0] == "hdparm":
            return 0, "Security:\n", ""
        return table.run(argv)

    backend = LinuxBackend(run_cmd=run, poll_interval=60, use_pyudev=False)
    backend.start(lambda s: None, lambda s: None)
    backend._reconcile()
    methods = backend.supported_wipe_methods("NVME-A1")
    assert methods == [WipeMethod.ZERO_OVERWRITE]
    backend.stop()


def test_sata_offers_enhanced_when_not_frozen(tmp_path):
    slots = load_slots_config()
    table = MutableDeviceTable([{
        "name": "sdc", "size": 251_000_193_024,
        "id_path": slots["SATA-1"].id_path,
    }])

    def run(argv):
        if argv[0] == "smartctl":
            return 0, json.dumps(_smart_identity()), ""
        if argv[0] == "hdparm":
            return 0, _hdparm_ok(), ""
        return table.run(argv)

    backend = LinuxBackend(run_cmd=run, poll_interval=60, use_pyudev=False)
    backend.start(lambda s: None, lambda s: None)
    backend._reconcile()
    methods = backend.supported_wipe_methods("SATA-1")
    assert WipeMethod.ATA_SECURE_ERASE_ENHANCED in methods
    assert WipeMethod.ZERO_OVERWRITE in methods
    backend.stop()


def test_sata_frozen_falls_back_to_overwrite(tmp_path):
    slots = load_slots_config()
    table = MutableDeviceTable([{
        "name": "sdc", "size": 251_000_193_024,
        "id_path": slots["SATA-1"].id_path,
    }])

    def run(argv):
        if argv[0] == "smartctl":
            return 0, json.dumps(_smart_identity()), ""
        if argv[0] == "hdparm":
            return 0, "Security:\n\tfrozen\n\tsupported: enhanced erase\n", ""
        return table.run(argv)

    backend = LinuxBackend(run_cmd=run, poll_interval=60, use_pyudev=False)
    backend.start(lambda s: None, lambda s: None)
    backend._reconcile()
    methods = backend.supported_wipe_methods("SATA-1")
    assert WipeMethod.ATA_SECURE_ERASE_ENHANCED not in methods
    assert WipeMethod.ZERO_OVERWRITE in methods
    backend.stop()


def test_station_ready_through_linux_backend(tmp_path):
    slots = load_slots_config()
    table = MutableDeviceTable([{
        "name": "sdc", "size": 251_000_193_024,
        "id_path": slots["SATA-1"].id_path,
    }])

    def run(argv):
        if argv[0] == "smartctl":
            return 0, json.dumps(_smart_identity(serial="LIVE1")), ""
        if argv[0] == "hdparm":
            return 0, _hdparm_ok(), ""
        return table.run(argv)

    backend = LinuxBackend(run_cmd=run, poll_interval=60, use_pyudev=False)
    joblog = JobLog(str(tmp_path / "t.db"))
    # start() runs initial reconcile — populate devices first
    station = Station(backend, joblog)
    # Force reconcile after station wired
    backend._reconcile()
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY via linux")
    assert slot.drive.serial == "LIVE1"
    backend.stop()
    joblog.close()
