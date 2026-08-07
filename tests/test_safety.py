"""These tests actively try to trick the station into wiping the wrong drive.
Every one of them must prove the destructive call never happened."""
import pytest

from drivestation.hw.simulator import SimFaults, make_nvme, make_sata_ssd
from drivestation.models import SlotStatus

from .conftest import wait_for


def test_non_allowlisted_device_is_ignored(station, backend):
    """A random USB stick (not on a dock) must be invisible and untouchable."""
    backend.insert_drive("ROGUE-USB", make_nvme(serial="INNOCENT"))
    assert "ROGUE-USB" not in station.slots
    assert all(s.status == SlotStatus.EMPTY for s in station.slots.values())
    with pytest.raises(ValueError):
        station.confirm_wipe("ROGUE-USB")
    assert backend.wipe_calls == []


def test_serial_swap_before_wipe_aborts(station, backend, joblog):
    """Drive identity changes between confirmation and the destructive
    command (drive swapped, or bridge lying). Must abort with NO wipe."""
    drive = make_nvme(serial="ORIGINAL",
                      faults=SimFaults(second_read_serial="SWAPPED"))
    backend.insert_drive("SUITOK-1", drive)
    slot = station.slots["SUITOK-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")

    station.confirm_wipe("SUITOK-1")
    wait_for(lambda: slot.status == SlotStatus.ERROR, message="ERROR")
    assert "DRIVE CHANGED" in slot.message
    assert backend.wipe_calls == []
    assert joblog.by_serial("ORIGINAL") == []
    assert joblog.by_serial("SWAPPED") == []


def test_cannot_wipe_slot_not_ready(station, backend):
    with pytest.raises(ValueError):
        station.confirm_wipe("SATA-1")  # empty slot
    backend.insert_drive("SATA-1", make_sata_ssd(serial="S1"))
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    station.confirm_wipe("SATA-1")
    with pytest.raises(ValueError):
        station.confirm_wipe("SATA-1")  # double-confirm must not queue twice
    wait_for(lambda: slot.status == SlotStatus.PASSED, message="PASSED")
    assert len(backend.wipe_calls) == 1


def test_duplicate_serials_in_two_slots(station, backend):
    """Two drives reporting the same serial must not confuse slot binding."""
    backend.insert_drive("NVME-A1", make_nvme(serial="DUP"))
    backend.insert_drive("NVME-B1", make_nvme(serial="DUP"))
    a1, b1 = station.slots["NVME-A1"], station.slots["NVME-B1"]
    wait_for(lambda: a1.status == SlotStatus.READY and
             b1.status == SlotStatus.READY, message="both READY")

    station.confirm_wipe("NVME-A1")
    wait_for(lambda: a1.status == SlotStatus.PASSED, message="A1 PASSED")
    assert backend.wipe_calls == [("NVME-A1", "DUP")]
    assert b1.status == SlotStatus.READY  # untouched


def test_unidentifiable_drive_never_wiped(station, backend):
    backend.insert_drive("M2-1", make_nvme(faults=SimFaults(identify_fails=True)))
    slot = station.slots["M2-1"]
    wait_for(lambda: slot.status == SlotStatus.ERROR, message="ERROR")
    assert not slot.awaiting_confirm
    with pytest.raises(ValueError):
        station.confirm_wipe("M2-1")
    assert backend.wipe_calls == []
