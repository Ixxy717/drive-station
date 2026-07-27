from __future__ import annotations

import time

import pytest

from drivestation.db import JobLog
from drivestation.hw.simulator import SimulatorBackend
from drivestation.models import SLOT_LAYOUT
from drivestation.station import Station


class InstrumentedSimulator(SimulatorBackend):
    """Records every destructive call so tests can prove a wipe never
    reached the wrong drive."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.wipe_calls: list[tuple[str, str]] = []  # (slot_id, serial)

    def wipe(self, slot_id, method, progress):
        drive = self._drive(slot_id)
        self.wipe_calls.append((slot_id, drive.info.serial))
        super().wipe(slot_id, method, progress)


def wait_for(condition, timeout: float = 5.0, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {message}")


@pytest.fixture
def backend():
    return InstrumentedSimulator([s for s, _ in SLOT_LAYOUT],
                                 wipe_duration=0.15, verify_duration=0.05)


@pytest.fixture
def joblog(tmp_path):
    log = JobLog(str(tmp_path / "test.db"))
    yield log
    log.close()


@pytest.fixture
def station(backend, joblog):
    return Station(backend, joblog)
