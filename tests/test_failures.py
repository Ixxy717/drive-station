"""Failure modes: every one must end loudly in FAILED/ERROR and never look
like a successful wipe."""
from drivestation.db import JobLog
from drivestation.hw.simulator import SimFaults, make_hdd, make_sata_ssd
from drivestation.models import SlotStatus
from drivestation.station import Station

from .conftest import InstrumentedSimulator, wait_for


def test_yank_mid_wipe(station, backend, joblog):
    backend.insert_drive("SATA-2",
                         make_hdd(serial="YANK", faults=SimFaults(disconnect_at=0.4)))
    slot = station.slots["SATA-2"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    station.confirm_wipe("SATA-2")
    wait_for(lambda: slot.status == SlotStatus.FAILED, message="FAILED")
    assert "disconnected" in slot.message.lower()

    record = joblog.by_serial("YANK")[0]
    assert record["result"] == "FAILED"
    assert "disconnected" in record["error"]
    # The failure stays on screen even though the slot is now empty:
    assert slot.status == SlotStatus.FAILED


def test_usb_blip_insert_ignored_during_wipe(station, backend, joblog):
    """Re-enumerate mid-wipe must not reset the slot / fake a disconnect."""
    backend.insert_drive("SATA-1", make_hdd(serial="BLIP"))
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    station.confirm_wipe("SATA-1")
    wait_for(lambda: slot.status == SlotStatus.WIPING, message="WIPING")
    # Hotplug "insert" while still wiping (USB bridge blip).
    station._on_insert("SATA-1")
    assert slot.status == SlotStatus.WIPING
    wait_for(lambda: slot.status == SlotStatus.PASSED, message="PASSED")
    assert "WIPED" in slot.message
    assert joblog.by_serial("BLIP")[0]["result"] == "PASSED"


def test_wipe_command_rejected(station, backend, joblog):
    backend.insert_drive("SATA-1",
                         make_sata_ssd(serial="REJ", faults=SimFaults(wipe_rejected=True)))
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    station.confirm_wipe("SATA-1")
    wait_for(lambda: slot.status == SlotStatus.FAILED, message="FAILED")
    assert joblog.by_serial("REJ")[0]["result"] == "FAILED"


def test_verify_failure_is_a_hard_fail(station, backend, joblog):
    backend.insert_drive("SATA-1",
                         make_hdd(serial="VF", faults=SimFaults(verify_fails=True)))
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    station.confirm_wipe("SATA-1")
    wait_for(lambda: slot.status == SlotStatus.FAILED, message="FAILED")
    assert "VERIFICATION" in slot.message
    assert joblog.by_serial("VF")[0]["result"] == "FAILED"


def test_power_loss_recovery(tmp_path):
    """A job cut off by power loss/crash must be FAILED on restart,
    never silently successful."""
    db_path = str(tmp_path / "recover.db")
    log1 = JobLog(db_path)
    log1.start_job(slot="SATA-1", manufacturer="WD", model="WD20EZRZ",
                   serial="PWRLOSS", capacity_bytes=2_000_000_000_000,
                   drive_type="SATA_HDD", health_percent=98,
                   health_verdict="GOOD", health_warnings=[],
                   wipe_method="ZERO_OVERWRITE")
    log1.close()  # power dies mid-wipe

    log2 = JobLog(db_path)
    backend = InstrumentedSimulator(["SATA-1"], wipe_duration=0.1)
    Station(backend, log2)  # startup recovery runs here
    record = log2.by_serial("PWRLOSS")[0]
    assert record["result"] == "FAILED"
    assert "re-wiped" in record["error"]
    log2.close()
