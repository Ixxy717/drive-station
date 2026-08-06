"""Queue wipe handoff: StarTech grading → SUITOK wipe-only bay."""
from drivestation.hw.simulator import make_nvme
from drivestation.models import LARGE_NVME_QUEUE_BYTES, SlotStatus

from .conftest import wait_for


def test_queue_wipe_then_wipe_only_dock_sees_it(station, backend, joblog):
    backend.insert_drive("NVME-A1", make_nvme(serial="BIG1"))
    a1 = station.slots["NVME-A1"]
    wait_for(lambda: a1.status == SlotStatus.READY, message="READY on A1")

    station.queue_wipe("NVME-A1")
    assert a1.awaiting_confirm is False
    assert "Queued" in a1.message
    assert station.pending_wipes() == [{"serial": "big1", "from_slot": "NVME-A1"}]

    backend.remove_drive("NVME-A1")
    wait_for(lambda: a1.status == SlotStatus.EMPTY, message="EMPTY A1")

    backend.insert_drive("NVME-C1", make_nvme(serial="BIG1"))
    c1 = station.slots["NVME-C1"]
    wait_for(lambda: c1.status == SlotStatus.READY, message="READY on C1")
    assert c1.wipe_only is True
    assert c1.queued_from == "NVME-A1"
    assert "Queued wipe from NVME-A1" in c1.message

    station.confirm_wipe("NVME-C1")
    wait_for(lambda: c1.status == SlotStatus.PASSED, message="PASSED")
    assert station.pending_wipes() == []
    assert joblog.by_serial("BIG1")[0]["result"] == "PASSED"


def test_large_nvme_wipe_auto_queues(station, backend):
    huge = make_nvme(serial="HUGE1")
    # Simulator DriveInfo is frozen-ish via dataclass — rebuild with 2TB.
    from drivestation.hw.simulator import SimDrive, SimFaults
    from drivestation.models import DriveInfo, DriveType

    drive = SimDrive(
        info=DriveInfo("Samsung", "PM9A1", "HUGE1",
                       LARGE_NVME_QUEUE_BYTES + 1, DriveType.NVME),
        health_raw={"percentage_used": 6},
        faults=SimFaults(),
    )
    backend.insert_drive("NVME-B1", drive)
    b1 = station.slots["NVME-B1"]
    wait_for(lambda: b1.status == SlotStatus.READY, message="READY")
    assert "≥1TB" in b1.message

    station.confirm_wipe("NVME-B1")  # should queue, not wipe
    assert backend.wipe_calls == []
    assert any(p["serial"] == "huge1" for p in station.pending_wipes())
    assert "queued" in b1.message.lower()


def test_wipe_here_forces_local_on_large(station, backend):
    from drivestation.hw.simulator import SimDrive, SimFaults
    from drivestation.models import DriveInfo, DriveType

    drive = SimDrive(
        info=DriveInfo("Samsung", "PM9A1", "HUGE2",
                       LARGE_NVME_QUEUE_BYTES + 1, DriveType.NVME),
        health_raw={"percentage_used": 6},
        faults=SimFaults(),
    )
    backend.insert_drive("NVME-A1", drive)
    a1 = station.slots["NVME-A1"]
    wait_for(lambda: a1.status == SlotStatus.READY, message="READY")
    station.confirm_wipe_here("NVME-A1")
    wait_for(lambda: a1.status == SlotStatus.PASSED, message="PASSED")
    assert backend.wipe_calls == [("NVME-A1", "HUGE2")]
