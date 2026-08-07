"""Real hardware backend for the station mini PC (Linux).

Locked to Phase 0 characterization — see config/slots.toml.
"""
from __future__ import annotations

import logging
import os
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

# USB bridges (RTL9210/ASM) often vanish for 1–3s mid-overwrite. Don't treat
# that as a yank until the slot has been continuously missing this long.
_REMOVE_DEBOUNCE_S = 5.0
_PRESENT_RETRY_S = 3.0


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
        # slot_id → monotonic time when it first disappeared from a scan
        self._gone_since: dict[str, float] = {}
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
        now = time.monotonic()
        removals: list[str] = []
        inserts: list[str] = []
        with self._lock:
            old_slots = set(self._present.keys())
            new_slots = set(found.keys())

            # Back online during debounce = a real re-seat. Must re-fire
            # on_insert (otherwise path updates silently and the board stays
            # EMPTY / stuck on the old identity).
            returned: list[str] = []
            for slot_id in new_slots:
                if slot_id in self._gone_since:
                    returned.append(slot_id)
                self._gone_since.pop(slot_id, None)

            for slot_id in sorted(old_slots - new_slots):
                since = self._gone_since.get(slot_id)
                if since is None:
                    self._gone_since[slot_id] = now
                    log.info(
                        "slot %s missing from scan — debounce %.0fs",
                        slot_id, _REMOVE_DEBOUNCE_S,
                    )
                    continue
                if now - since < _REMOVE_DEBOUNCE_S:
                    continue
                self._gone_since.pop(slot_id, None)
                self._present.pop(slot_id, None)
                self._dev_by_slot.pop(slot_id, None)
                log.info("slot %s empty/removed (debounced)", slot_id)
                removals.append(slot_id)

            for slot_id in sorted(new_slots - old_slots):
                self._present[slot_id] = found[slot_id]
                self._dev_by_slot[slot_id] = found[slot_id].path
                log.info("slot %s present at %s", slot_id, found[slot_id].path)
                inserts.append(slot_id)

            # Same slot still listed — update path; re-insert if media changed
            # (capacity) or if it blinked away and came back during debounce.
            for slot_id in sorted(new_slots & old_slots):
                old = self._present[slot_id]
                new = found[slot_id]
                self._present[slot_id] = new
                self._dev_by_slot[slot_id] = new.path
                size_changed = old.size_bytes != new.size_bytes
                if size_changed or slot_id in returned:
                    log.info(
                        "slot %s media change (size %s→%s, returned=%s) "
                        "— re-identify",
                        slot_id, old.size_bytes, new.size_bytes,
                        slot_id in returned,
                    )
                    if slot_id not in inserts:
                        inserts.append(slot_id)

        for slot_id in removals:
            try:
                self._on_remove(slot_id)
            except Exception:
                log.exception("on_remove(%s) failed", slot_id)
        for slot_id in inserts:
            try:
                self._on_insert(slot_id)
            except Exception:
                log.exception("on_insert(%s) failed", slot_id)

    def hw_debug(self) -> dict:
        """Live allowlist / presence snapshot for /api/debug/hw."""
        from .sysfs import list_block_disks
        disks = []
        for d in list_block_disks(self._run):
            mapped = self._path_to_slot.get(d.id_path)
            disks.append({
                "path": d.path,
                "size_bytes": d.size_bytes,
                "id_path": d.id_path,
                "mapped_slot": mapped,
                "allowlisted_active": (
                    mapped is not None and d.size_bytes > 0
                ),
            })
        with self._lock:
            present = {
                sid: {"path": dev.path, "size_bytes": dev.size_bytes}
                for sid, dev in self._present.items()
            }
            gone = dict(self._gone_since)
        return {
            "disks": disks,
            "present_slots": present,
            "gone_since": {
                sid: round(time.monotonic() - t, 1) for sid, t in gone.items()
            },
            "configured_slots": {
                sid: {"id_path": cfg.id_path, "bridge": cfg.bridge}
                for sid, cfg in self.slots.items()
            },
        }

    def _require_dev(self, slot_id: str) -> str:
        with self._lock:
            path = self._dev_by_slot.get(slot_id)
        if not path:
            raise DriveDisconnected(f"No drive present in {slot_id}")
        return path

    def _refresh_dev(self, slot_id: str) -> str:
        """Re-resolve /dev path from ID_PATH (USB bridges renumber after wipe)."""
        found = scan_allowlisted(self._path_to_slot, self._run)
        cur = found.get(slot_id)
        if cur is None or cur.size_bytes <= 0:
            raise DriveDisconnected(f"No drive present in {slot_id}")
        with self._lock:
            self._present[slot_id] = cur
            self._dev_by_slot[slot_id] = cur.path
            self._gone_since.pop(slot_id, None)
        return cur.path

    def _slot_cfg(self, slot_id: str) -> SlotConfig:
        return self.slots[slot_id]

    def _still_present(self, slot_id: str, dev_path: str) -> bool:
        """True unless the slot stays gone for _PRESENT_RETRY_S.

        A single missed udev/lsblk sample must not kill an active wipe.
        """
        deadline = time.monotonic() + _PRESENT_RETRY_S
        while True:
            found = scan_allowlisted(self._path_to_slot, self._run)
            cur = found.get(slot_id)
            if cur is not None and cur.size_bytes > 0:
                with self._lock:
                    self._present[slot_id] = cur
                    self._dev_by_slot[slot_id] = cur.path
                    self._gone_since.pop(slot_id, None)
                return True
            # Scan can miss ID_PATH while dd still holds the original node.
            try:
                if dev_path and os.path.exists(dev_path):
                    return True
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.35)

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
        # Locked drives: still offer ATA erase (NULL-password bypass attempt)
        # then zero overwrite as fallback.
        if cfg.bridge in ("asmedia_sata", "rtl9220"):
            state = ata_security_state(path, self._run)
            if state["enhanced_erase"] and not state["frozen"]:
                methods.append(WipeMethod.ATA_SECURE_ERASE_ENHANCED)
            if not state["frozen"]:
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
            try:
                ata_enhanced_erase(path, progress, self._run, poll_present=poll)
                return
            except WipeError as exc:
                # Locked/failed ATA path → still try overwrite so the bay
                # is not a dead end when security flags block set-pass.
                msg = str(exc).lower()
                if "locked" in msg or "frozen" in msg:
                    log.warning(
                        "ATA erase failed on %s (%s); falling back to overwrite",
                        slot_id, exc)
                    zero_overwrite(path, progress, self._run, poll_present=poll)
                    return
                raise
        if method == WipeMethod.ZERO_OVERWRITE:
            zero_overwrite(path, progress, self._run, poll_present=poll)
            return
        raise WipeError(f"wipe method {method.value} not enabled on this station")

    def verify(self, slot_id: str, method: WipeMethod,
               progress: ProgressCallback) -> None:
        # Full overwrite often resets USB mass-storage — wait and re-resolve
        # the /dev node before opening for verify (avoids false ENXIO fails).
        path = None
        last: Optional[Exception] = None
        for attempt in range(8):
            try:
                if attempt:
                    time.sleep(0.75)
                path = self._refresh_dev(slot_id)
                break
            except DriveDisconnected as exc:
                last = exc
        if path is None:
            raise last or DriveDisconnected(f"No drive present in {slot_id}")

        if method == WipeMethod.ZERO_OVERWRITE:
            verify_zeros(path, progress, self._run)
            return
        if method in (WipeMethod.ATA_SECURE_ERASE_ENHANCED,
                      WipeMethod.ATA_SECURE_ERASE):
            verify_ata_erase(path, progress, self._run)
            return
        from .base import VerifyError
        raise VerifyError(f"no verify policy for {method.value}")
