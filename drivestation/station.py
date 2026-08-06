from __future__ import annotations

import logging
import threading
from typing import Optional

from .db import JobLog
from .health.policy import evaluate_health
from .hw.base import (DriveDisconnected, HardwareBackend, HardwareError,
                      VerifyError, WipeError)
from .models import (SLOT_LAYOUT, DriveInfo, HealthVerdict, SlotState,
                     SlotStatus, UsageSnapshot, WipeMethod)
from .wipe.methods import choose_method

log = logging.getLogger("drivestation")


class Station:
    """Per-slot state machine and safety-gated wipe engine.

    Safety model:
      * Only slots in the layout (i.e. allowlisted docks) exist at all;
        events for anything else are ignored and logged.
      * A wipe job is bound to (slot, serial) at confirmation time.
      * The serial is re-read from hardware immediately before the destructive
        command; any mismatch or ambiguity aborts to ERROR without wiping.
      * A drive vanishing mid-job fails the job loudly with the slot name.
    """

    def __init__(self, backend: HardwareBackend, joblog: JobLog,
                 batch: Optional[str] = None):
        self.backend = backend
        self.joblog = joblog
        self.batch = batch
        self._lock = threading.RLock()
        self.slots: dict[str, SlotState] = {
            slot_id: SlotState(slot_id=slot_id, group=group)
            for slot_id, group in SLOT_LAYOUT
        }
        interrupted = self.joblog.mark_interrupted_jobs()
        if interrupted:
            log.warning("Marked %d interrupted job(s) as FAILED after restart",
                        interrupted)
        backend.start(self._on_insert, self._on_remove)

    # -- hot-plug events -----------------------------------------------------

    def _on_insert(self, slot_id: str) -> None:
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                log.warning("Ignoring device on non-allowlisted slot %r", slot_id)
                return
            slot.status = SlotStatus.DETECTED
            slot.drive = None
            slot.health = None
            slot.usage = None
            slot.progress = 0.0
            slot.message = ""
            slot.awaiting_confirm = False
            slot.wipe_method = None
        threading.Thread(target=self._check, args=(slot_id,),
                         name=f"check-{slot_id}", daemon=True).start()

    def _on_remove(self, slot_id: str) -> None:
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                return
            if slot.status in (SlotStatus.WIPING, SlotStatus.VERIFYING):
                # The wipe thread also detects this and logs the job; the
                # state is set here too so the UI reacts instantly.
                slot.status = SlotStatus.FAILED
                slot.message = "Drive disconnected during wipe"
                slot.awaiting_confirm = False
            elif slot.status == SlotStatus.FAILED and "disconnected" in slot.message.lower():
                pass  # keep the failure visible until the next insertion
            else:
                # Normal removal (finished or declined drive pulled out).
                slot.status = SlotStatus.EMPTY
                slot.drive = None
                slot.health = None
                slot.usage = None
                slot.progress = 0.0
                slot.message = ""
                slot.awaiting_confirm = False
                slot.wipe_method = None

    # -- health check --------------------------------------------------------

    def _check(self, slot_id: str) -> None:
        slot = self.slots[slot_id]
        with self._lock:
            if slot.status != SlotStatus.DETECTED:
                return
            slot.status = SlotStatus.CHECKING
        try:
            info = self.backend.read_identity(slot_id)
            if info is None:
                with self._lock:
                    slot.status = SlotStatus.ERROR
                    slot.message = "Drive failed to identify — try reseating it"
                return
            raw = self.backend.read_health(slot_id)
            health = evaluate_health(info, raw)
            usage_raw = self.backend.read_usage(slot_id) or {}
            usage = None
            if usage_raw:
                usage = UsageSnapshot(
                    capacity_bytes=int(usage_raw.get("capacity_bytes")
                                       or info.capacity_bytes or 0),
                    used_bytes=usage_raw.get("used_bytes"),
                    has_partitions=bool(usage_raw.get("has_partitions")),
                    label=str(usage_raw.get("label") or ""),
                    detail=str(usage_raw.get("detail") or ""),
                )
            # Preview which wipe method will run, so the operator sees
            # "ZERO OVERWRITE" vs "ATA SECURE ERASE" before confirming.
            planned: Optional[WipeMethod] = None
            try:
                planned = choose_method(
                    info.drive_type,
                    self.backend.supported_wipe_methods(slot_id))
            except HardwareError:
                pass
            with self._lock:
                slot.drive = info
                slot.health = health
                slot.usage = usage
                slot.wipe_method = planned
                slot.status = SlotStatus.READY
                slot.awaiting_confirm = True
                if health.verdict in (HealthVerdict.SCRAP, HealthVerdict.FAIL):
                    slot.message = health.warnings[0] if health.warnings \
                        else "SCRAP — do not resell"
                else:
                    slot.message = ""
        except DriveDisconnected:
            pass  # removal event resets the slot
        except HardwareError as exc:
            with self._lock:
                slot.status = SlotStatus.ERROR
                slot.message = f"Health check error: {exc}"

    # -- operator actions ----------------------------------------------------

    def confirm_wipe(self, slot_id: str) -> None:
        """Operator pressed YES. All safety gates run before anything
        destructive happens."""
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                raise ValueError(f"Unknown slot {slot_id!r}")
            if slot.status != SlotStatus.READY or not slot.awaiting_confirm:
                raise ValueError(f"{slot_id} is not awaiting wipe confirmation")
            if slot.drive is None:
                raise ValueError(f"{slot_id} has no bound drive identity")
            bound = slot.drive
            slot.awaiting_confirm = False
        threading.Thread(target=self._wipe_job, args=(slot_id, bound),
                         name=f"wipe-{slot_id}", daemon=True).start()

    def decline_wipe(self, slot_id: str) -> None:
        """Operator pressed NO. Nothing happens to the drive."""
        with self._lock:
            slot = self.slots[slot_id]
            if slot.status == SlotStatus.READY:
                slot.awaiting_confirm = False
                slot.message = "Not wiped — remove drive"

    # -- wipe engine ---------------------------------------------------------

    def _fail(self, slot: SlotState, message: str,
              job_id: Optional[int], error: str,
              status: SlotStatus = SlotStatus.FAILED) -> None:
        # Log first, then update the screen: a result must never be visible
        # to the operator before it is durably recorded.
        if job_id is not None:
            self.joblog.finish_job(job_id, "FAILED", error)
        with self._lock:
            slot.status = status
            slot.message = message

    def _wipe_job(self, slot_id: str, bound: DriveInfo) -> None:
        slot = self.slots[slot_id]
        job_id: Optional[int] = None
        try:
            # SAFETY GATE 1: re-read identity fresh from hardware and compare
            # against the identity bound at confirmation time.
            current = self.backend.read_identity(slot_id)
            if current is None:
                self._fail(slot, "Drive identity unreadable — wipe aborted",
                           job_id, "identity unreadable before wipe",
                           SlotStatus.ERROR)
                return
            if current.serial != bound.serial:
                self._fail(
                    slot,
                    "DRIVE CHANGED — wipe aborted. Remove and re-insert.",
                    job_id,
                    f"serial mismatch before wipe: bound={bound.serial} "
                    f"current={current.serial}",
                    SlotStatus.ERROR)
                return

            # SAFETY GATE 2: a wipe method must be explicitly supported for
            # this drive through this dock. No method, no wipe.
            method = choose_method(bound.drive_type,
                                   self.backend.supported_wipe_methods(slot_id))
            if method is None:
                self._fail(slot, "No supported wipe method for this drive",
                           job_id, "no supported wipe method", SlotStatus.ERROR)
                return

            health = slot.health
            usage = slot.usage
            job_id = self.joblog.start_job(
                slot=slot_id, manufacturer=bound.manufacturer,
                model=bound.model, serial=bound.serial,
                capacity_bytes=bound.capacity_bytes,
                drive_type=bound.drive_type.value,
                health_percent=health.percent if health else None,
                health_verdict=health.verdict.value if health else "UNKNOWN",
                health_warnings=health.warnings if health else [],
                wipe_method=method.value, batch=self.batch,
                used_bytes_before=usage.used_bytes if usage else None,
                usage_label=usage.label if usage else None)

            with self._lock:
                slot.status = SlotStatus.WIPING
                slot.wipe_method = method
                slot.progress = 0.0
                slot.message = ""

            def wipe_progress(frac: float) -> None:
                with self._lock:
                    slot.progress = frac

            self.backend.wipe(slot_id, method, wipe_progress)

            with self._lock:
                if slot.status != SlotStatus.WIPING:
                    # Removal event won the race; treat as disconnect.
                    raise DriveDisconnected("state changed during wipe")
                slot.status = SlotStatus.VERIFYING
                slot.progress = 0.0

            def verify_progress(frac: float) -> None:
                with self._lock:
                    slot.progress = frac

            self.backend.verify(slot_id, method, verify_progress)

            # SAFETY/AUDIT GATE 3: the same drive must still be present after
            # verification for the job to be recorded as a success.
            after = self.backend.read_identity(slot_id)
            if after is None or after.serial != bound.serial:
                raise WipeError("drive identity changed during job")

            # Log first, then update the screen (never show PASSED before the
            # record is durably committed).
            self.joblog.finish_job(job_id, "PASSED")
            with self._lock:
                slot.status = SlotStatus.PASSED
                slot.progress = 1.0
                slot.message = (
                    f"Wiped and verified — listing /d/{bound.serial}"
                )

        except DriveDisconnected:
            self._fail(slot, "Drive disconnected during wipe",
                       job_id, "drive disconnected during wipe")
        except VerifyError as exc:
            self._fail(slot, "WIPE VERIFICATION FAILED — do not resell",
                       job_id, f"verify failed: {exc}")
        except WipeError as exc:
            self._fail(slot, f"Wipe failed: {exc}", job_id, f"wipe failed: {exc}")
        except Exception as exc:  # never let a bug look like success
            log.exception("Unexpected error in wipe job for %s", slot_id)
            self._fail(slot, "Unexpected error — drive NOT verified wiped",
                       job_id, f"unexpected: {exc}", SlotStatus.ERROR)

    # -- UI state ------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [self.slots[slot_id].to_dict()
                    for slot_id, _ in SLOT_LAYOUT]
