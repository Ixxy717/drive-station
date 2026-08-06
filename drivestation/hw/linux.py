"""Real hardware backend for the station mini PC (Linux).

Locked to Phase 0 characterization — see config/slots.toml.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..models import DriveInfo, DriveType, WipeMethod
from .base import (DriveDisconnected, HardwareBackend, ProgressCallback,
                   WipeError)
from .identify import ata_security_state, read_health, read_identity
from .slots_config import (SlotConfig, SlotsConfigError, load_slots_config,
                           path_to_slot)
from .sysfs import RunCmd, BlockDevice, default_run_cmd, scan_allowlisted
from .usage import read_usage
from .wipe_linux import (ata_enhanced_erase, verify_ata_erase, verify_zeros,
                         zero_overwrite)

log = logging.getLogger("drivestation.linux")


class LinuxBackend(HardwareBackend):
    """udev/poll detection + smartctl/hdparm/dd against allowlisted docks."""

    def __init__(
        self,
        slots_path: Optional[Path] = None,
        run_cmd: RunCmd = default_run_cmd,
        poll_interval: float = 2.0,
        use_pyudev: bool = True,
    ):
        try:
            self.slots = load_slots_config(slots_path)
        except (SlotsConfigError, OSError) as exc:
            raise RuntimeError(str(exc)) from exc
        self._path_to_slot = path_to_slot(self.slots)
        self._run = run_cmd
        self._poll_interval = poll_interval
        self._use_pyudev = use_pyudev
        self._on_insert: Callable[[str], None] = lambda s: None
        self._on_remove: Callable[[str], None] = lambda s: None
        self._lock = threading.RLock()
        # slot_id → BlockDevice currently considered present (size > 0)
        self._present: dict[str, BlockDevice] = {}
        self._dev_by_slot: dict[str, str] = {}  # slot → /dev/sdX
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, on_insert, on_remove) -> None:
        self._on_insert = on_insert
        self._on_remove = on_remove
        # Initial scan
        self._reconcile()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="linux-hw-poll", daemon=True)
        self._thread.start()
        if self._use_pyudev:
            threading.Thread(
                target=self._udev_loop, name="linux-udev", daemon=True,
            ).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self._reconcile()
            except Exception:
                log.exception("poll reconcile failed")

    def _udev_loop(self) -> None:
        try:
            import pyudev
        except ImportError:
            log.info("pyudev not available; relying on poll only")
            return
        try:
            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by(subsystem="block")
            monitor.start()
            while not self._stop.is_set():
                device = monitor.poll(timeout=1.0)
                if device is not None:
                    self._reconcile()
        except Exception:
            log.exception("udev monitor failed; continuing with poll only")

    def _reconcile(self) -> None:
        found = scan_allowlisted(self._path_to_slot, self._run)
        with self._lock:
            old_slots = set(self._present.keys())
            new_slots = set(found.keys())

            for slot_id in sorted(old_slots - new_slots):
                self._present.pop(slot_id, None)
                self._dev_by_slot.pop(slot_id, None)
                log.info("slot %s empty/removed", slot_id)
                try:
                    self._on_remove(slot_id)
                except Exception:
                    log.exception("on_remove(%s) failed", slot_id)

            for slot_id in sorted(new_slots - old_slots):
                self._present[slot_id] = found[slot_id]
                self._dev_by_slot[slot_id] = found[slot_id].path
                log.info("slot %s present at %s", slot_id, found[slot_id].path)
                try:
                    self._on_insert(slot_id)
                except Exception:
                    log.exception("on_insert(%s) failed", slot_id)

            # Same slot, device node renamed (rare) — update path only
            for slot_id in new_slots & old_slots:
                self._present[slot_id] = found[slot_id]
                self._dev_by_slot[slot_id] = found[slot_id].path

    def _require_dev(self, slot_id: str) -> str:
        with self._lock:
            path = self._dev_by_slot.get(slot_id)
        if not path:
            raise DriveDisconnected(f"No drive present in {slot_id}")
        return path

    def _slot_cfg(self, slot_id: str) -> SlotConfig:
        return self.slots[slot_id]

    def _still_present(self, slot_id: str, dev_path: str) -> bool:
        found = scan_allowlisted(self._path_to_slot, self._run)
        cur = found.get(slot_id)
        return cur is not None and cur.path == dev_path and cur.size_bytes > 0

    def read_identity(self, slot_id: str) -> Optional[DriveInfo]:
        path = self._require_dev(slot_id)
        with self._lock:
            dev = self._present.get(slot_id)
        fallback = dev.size_bytes if dev else 0
        return read_identity(
            path, self._slot_cfg(slot_id), self._run,
            fallback_capacity_bytes=fallback,
        )

    def read_health(self, slot_id: str) -> dict:
        path = self._require_dev(slot_id)
        info = read_identity(path, self._slot_cfg(slot_id), self._run)
        dtype = info.drive_type if info else DriveType.UNKNOWN
        return read_health(path, self._slot_cfg(slot_id), dtype, self._run)

    def read_usage(self, slot_id: str) -> dict:
        path = self._require_dev(slot_id)
        info = read_identity(path, self._slot_cfg(slot_id), self._run)
        cap = info.capacity_bytes if info else 0
        return read_usage(path, cap, self._run).to_dict()

    def supported_wipe_methods(self, slot_id: str) -> list[WipeMethod]:
        cfg = self._slot_cfg(slot_id)
        path = self._require_dev(slot_id)
        info = read_identity(path, cfg, self._run)
        methods: list[WipeMethod] = []

        # NVMe-through-USB or NVMe in M2: overwrite only
        if info and info.drive_type == DriveType.NVME:
            return [WipeMethod.ZERO_OVERWRITE]
        if cfg.bridge in ("rtl9210", "asm2362"):
            return [WipeMethod.ZERO_OVERWRITE]

        # SAS through a USB enclosure: no ATA security; overwrite only
        if cfg.bridge == "sas_usb" or (
                info and info.drive_type in (DriveType.SAS_HDD,
                                             DriveType.SAS_SSD)):
            return [WipeMethod.ZERO_OVERWRITE]

        # ASMedia SATA + RTL9220 SATA media: enhanced erase when available.
        # Locked drives need a user password we don't have — overwrite only
        # (and even that may I/O-error until unlocked).
        if cfg.bridge in ("asmedia_sata", "rtl9220"):
            state = ata_security_state(path, self._run)
            if state.get("locked"):
                return [WipeMethod.ZERO_OVERWRITE]
            if state["enhanced_erase"] and not state["frozen"]:
                methods.append(WipeMethod.ATA_SECURE_ERASE_ENHANCED)
            if not state["frozen"]:
                # normal erase as secondary if enhanced missing
                if WipeMethod.ATA_SECURE_ERASE_ENHANCED not in methods:
                    methods.append(WipeMethod.ATA_SECURE_ERASE)
            methods.append(WipeMethod.ZERO_OVERWRITE)
            return methods

        return [WipeMethod.ZERO_OVERWRITE]

    def wipe(self, slot_id: str, method: WipeMethod,
             progress: ProgressCallback) -> None:
        path = self._require_dev(slot_id)

        def poll() -> bool:
            return self._still_present(slot_id, path)

        if method in (WipeMethod.ATA_SECURE_ERASE_ENHANCED,
                      WipeMethod.ATA_SECURE_ERASE):
            # v1: only enhanced path implemented; map normal to enhanced attempt
            ata_enhanced_erase(path, progress, self._run, poll_present=poll)
            return
        if method == WipeMethod.ZERO_OVERWRITE:
            zero_overwrite(path, progress, self._run, poll_present=poll)
            return
        raise WipeError(f"wipe method {method.value} not enabled on this station")

    def verify(self, slot_id: str, method: WipeMethod,
               progress: ProgressCallback) -> None:
        path = self._require_dev(slot_id)
        if method == WipeMethod.ZERO_OVERWRITE:
            verify_zeros(path, progress, self._run)
            return
        if method in (WipeMethod.ATA_SECURE_ERASE_ENHANCED,
                      WipeMethod.ATA_SECURE_ERASE):
            verify_ata_erase(path, progress, self._run)
            return
        from .base import VerifyError
        raise VerifyError(f"no verify policy for {method.value}")
