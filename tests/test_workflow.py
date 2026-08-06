"""Happy-path operator workflow."""
from drivestation.hw.simulator import make_hdd, make_nvme
from drivestation.models import SlotStatus

from .conftest import wait_for


def test_insert_check_wipe_pass(station, backend, joblog):
    backend.insert_drive("NVME-A1", make_nvme(percentage_used=6, serial="NV1"))
    slot = station.slots["NVME-A1"]

    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    assert slot.drive.serial == "NV1"
    assert slot.health.percent == 94
    assert slot.awaiting_confirm

    station.confirm_wipe("NVME-A1")
    wait_for(lambda: slot.status == SlotStatus.PASSED, message="PASSED")

    records = joblog.by_serial("NV1")
    assert len(records) == 1
    assert records[0]["result"] == "PASSED"
    assert records[0]["slot"] == "NVME-A1"
    assert records[0]["wipe_method"] == "NVME_SANITIZE_CRYPTO"

    backend.remove_drive("NVME-A1")
    wait_for(lambda: slot.status == SlotStatus.EMPTY, message="EMPTY after removal")


def test_decline_leaves_drive_untouched(station, backend, joblog):
    backend.insert_drive("SATA-1", make_hdd(serial="HD1"))
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")

    station.decline_wipe("SATA-1")
    assert not slot.awaiting_confirm
    assert backend.wipe_calls == []
    assert joblog.by_serial("HD1") == []

    backend.remove_drive("SATA-1")
    wait_for(lambda: slot.status == SlotStatus.EMPTY, message="EMPTY")


def test_all_slots_run_independently(station, backend):
    backend.insert_drive("NVME-A1", make_nvme(serial="P1"))
    backend.insert_drive("NVME-B1", make_nvme(serial="P2"))
    a1, b1 = station.slots["NVME-A1"], station.slots["NVME-B1"]
    wait_for(lambda: a1.status == SlotStatus.READY and
             b1.status == SlotStatus.READY, message="both READY")

    station.confirm_wipe("NVME-A1")
    station.confirm_wipe("NVME-B1")
    wait_for(lambda: a1.status == SlotStatus.PASSED and
             b1.status == SlotStatus.PASSED, message="both PASSED")
    assert sorted(backend.wipe_calls) == [("NVME-A1", "P1"), ("NVME-B1", "P2")]
