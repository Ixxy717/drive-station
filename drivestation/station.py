from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .db import JobLog
from .health.policy import evaluate_health
from .hw.base import (DriveDisconnected, HardwareBackend, HardwareError,
                      VerifyError, WipeError)
from .models import (LARGE_NVME_QUEUE_BYTES, SLOT_LAYOUT, WIPE_ONLY_SLOTS,
                     DriveInfo, DriveType, HealthVerdict, SlotState,
                     SlotStatus, UsageSnapshot, WipeMethod)
from .wipe.methods import choose_method

log = logging.getLogger("drivestation")


def _usage_looks_clean(usage: Optional[UsageSnapshot]) -> bool:
    if usage is None:
        return False
    if usage.has_partitions:
        return False
    if usage.used_bytes is not None and usage.used_bytes > 0:
        return False
    label = (usage.label or "").lower()
    if "data present" in label or "unreadable" in label:
        return False
    return "empty" in label or usage.used_bytes == 0


def _prior_wipe_info(jobs: list[dict],
                     usage: Optional[UsageSnapshot]) -> Optional[dict]:
    if not jobs:
        return None
    last = jobs[0]
    last_pass = next((j for j in jobs if j.get("result") == "PASSED"), None)
    still_clean = bool(last_pass and _usage_looks_clean(usage))
    src = last_pass or last
    return {
        "result": src.get("result"),
        "finished_at": src.get("wipe_finished_at") or src.get("created_at"),
        "method": src.get("wipe_method"),
        "slot": src.get("slot"),
        "still_clean": still_clean,
        "had_passed": last_pass is not None,
        "last_result": last.get("result"),
    }


class Station:
    """Per-slot state machine and safety-gated wipe engine.

    Safety model:
      * Only slots in the layout (i.e. allowlisted docks) exist at all;
        events for anything else are ignored and logged.
      * A wipe job is bound to (slot, serial) at confirmation time.
      * The serial is re-read from hardware immediately before the destructive
        command; any mismatch or ambiguity aborts to ERROR without wiping.
      * A drive vanishing mid-job fails the job loudly with the slot name.

    Wipe-only docks (SUITOK): a serial queued from a grading dock (StarTech)
    auto-starts wipe on insert. Other sticks still ask for confirm.
    """

    def __init__(self, backend: HardwareBackend, joblog: JobLog,
                 batch: Optional[str] = None):
        self.backend = backend
        self.joblog = joblog
        self.batch = batch
        self._lock = threading.RLock()
        self.slots: dict[str, SlotState] = {
            slot_id: SlotState(
                slot_id=slot_id, group=group,
                wipe_only=slot_id in WIPE_ONLY_SLOTS)
            for slot_id, group in SLOT_LAYOUT
        }
        # serial (casefold) → grading slot that queued it (also in SQLite)
        self._pending_wipes: dict[str, str] = self.joblog.load_pending_wipes()
        # Per-slot generation so a slow identify cannot overwrite a newer insert.
        self._check_gen: dict[str, int] = {}
        if self._pending_wipes:
            log.info("Restored %d pending wipe(s) from database",
                     len(self._pending_wipes))
        interrupted = self.joblog.mark_interrupted_jobs()
        if interrupted:
            log.warning("Marked %d interrupted job(s) as FAILED after restart",
                        interrupted)
        backend.start(self._on_insert, self._on_remove)

    def _queue_serial(self, serial: str, from_slot: str) -> None:
        key = serial.casefold()
        self._pending_wipes[key] = from_slot
        self.joblog.set_pending_wipe(serial, from_slot)

    def _dequeue_serial(self, serial: str) -> None:
        self._pending_wipes.pop(serial.casefold(), None)
        self.joblog.clear_pending_wipe(serial)

    # -- hot-plug events -----------------------------------------------------

    def _on_insert(self, slot_id: str) -> None:
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                log.warning("Ignoring device on non-allowlisted slot %r", slot_id)
                return
            # USB bridges re-enumerate mid-overwrite. Resetting the slot here
            # aborts a live wipe with a fake "disconnected" failure.
            if slot.status in (SlotStatus.WIPING, SlotStatus.VERIFYING):
                log.info(
                    "Ignoring insert on %s during %s (USB re-enumerate)",
                    slot_id, slot.status.value,
                )
                return
            gen = self._check_gen.get(slot_id, 0) + 1
            self._check_gen[slot_id] = gen
            slot.status = SlotStatus.DETECTED
            slot.drive = None
            slot.health = None
            slot.usage = None
            slot.progress = 0.0
            slot.wipe_elapsed_s = None
            slot.wipe_eta_s = None
            slot.wipe_bps = None
            slot.message = ""
            slot.awaiting_confirm = False
            slot.wipe_method = None
            slot.queued_from = None
            slot.prior_wipe = None
        threading.Thread(target=self._check, args=(slot_id, gen),
                         name=f"check-{slot_id}", daemon=True).start()

    def _on_remove(self, slot_id: str) -> None:
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                return
            if slot.status in (SlotStatus.WIPING, SlotStatus.VERIFYING):
                # Do not flip the tile to FAILED on a hotplug blip — the wipe
                # thread decides after present-check retries. Real yanks still
                # fail there once the device stays gone.
                log.warning(
                    "Ignoring remove on %s during %s (likely USB blip)",
                    slot_id, slot.status.value,
                )
                return
            elif slot.status == SlotStatus.FAILED and "disconnected" in slot.message.lower():
                pass  # keep the failure visible until the next insertion
            else:
                # Normal removal (finished or declined drive pulled out).
                # Pending wipe queue is kept by serial so a move to a wipe
                # dock still matches after yanking from the grading bay.
                slot.status = SlotStatus.EMPTY
                slot.drive = None
                slot.health = None
                slot.usage = None
                slot.progress = 0.0
                slot.wipe_elapsed_s = None
                slot.wipe_eta_s = None
                slot.wipe_bps = None
                slot.message = ""
                slot.awaiting_confirm = False
                slot.wipe_method = None
                slot.queued_from = None
                slot.prior_wipe = None

    # -- health check --------------------------------------------------------

    def _check(self, slot_id: str, gen: int) -> None:
        slot = self.slots[slot_id]
        with self._lock:
            if self._check_gen.get(slot_id) != gen:
                return
            if slot.status != SlotStatus.DETECTED:
                return
            slot.status = SlotStatus.CHECKING
        try:
            info = self.backend.read_identity(slot_id)
            if info is None:
                with self._lock:
                    if self._check_gen.get(slot_id) != gen:
                        return
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

            prior = _prior_wipe_info(
                self.joblog.by_serial(info.serial), usage)

            auto_start = False
            with self._lock:
                if self._check_gen.get(slot_id) != gen:
                    return
                slot.drive = info
                slot.health = health
                slot.usage = usage
                slot.wipe_method = planned
                slot.prior_wipe = prior
                queued_from = self._pending_wipes.get(info.serial.casefold())
                slot.queued_from = queued_from
                msgs: list[str] = []
                if queued_from and slot.wipe_only:
                    # Queued from a grading dock → start wipe immediately.
                    # Skip auto-start when a prior PASSED wipe still looks clean
                    # (operator can still press WIPE).
                    if prior and prior.get("still_clean"):
                        slot.status = SlotStatus.READY
                        slot.awaiting_confirm = True
                        when = (prior.get("finished_at") or "")[:10]
                        msgs.append(
                            f"Already wiped PASSED {when} — still empty"
                        )
                        msgs.append(f"was queued from {queued_from}")
                        slot.message = " · ".join(msgs)
                    else:
                        slot.status = SlotStatus.READY
                        slot.awaiting_confirm = False
                        msgs.append(f"Queued wipe from {queued_from} — starting")
                        if health.verdict == HealthVerdict.UNKNOWN:
                            msgs.append("health not graded on this dock")
                        slot.message = " · ".join(msgs)
                        auto_start = True
                else:
                    slot.status = SlotStatus.READY
                    slot.awaiting_confirm = True
                    if prior and prior.get("still_clean"):
                        when = (prior.get("finished_at") or "")[:10]
                        msgs.append(
                            f"Already wiped PASSED {when} — still empty"
                        )
                    elif prior and prior.get("had_passed"):
                        when = (prior.get("finished_at") or "")[:10]
                        msgs.append(
                            f"Previously wiped {when} but data present — rewipe"
                        )
                    elif prior and prior.get("last_result") == "FAILED":
                        msgs.append("Last wipe FAILED — needs a clean pass")
                    if (not slot.wipe_only
                            and info.drive_type == DriveType.NVME
                            and info.capacity_bytes >= LARGE_NVME_QUEUE_BYTES):
                        msgs.append(
                            "≥1TB — queue to WIPE ONLY dock (or wipe here)")
                    if health.verdict in (HealthVerdict.SCRAP, HealthVerdict.FAIL):
                        msgs.append(health.warnings[0] if health.warnings
                                    else "SCRAP — do not resell")
                    elif (health.verdict == HealthVerdict.UNKNOWN
                          and slot.wipe_only):
                        msgs.append(
                            "Wipe only — health not graded on this dock")
                    # Surface ATA locked/frozen even when health is otherwise OK.
                    for w in (health.warnings or []):
                        if "LOCKED" in w or "frozen" in w.lower():
                            if w not in msgs:
                                msgs.append(w)
                    slot.message = " · ".join(msgs)

            if auto_start:
                log.info("Auto-starting queued wipe for %s on %s (from %s)",
                         info.serial, slot_id, queued_from)
                threading.Thread(
                    target=self._wipe_job, args=(slot_id, info),
                    name=f"wipe-{slot_id}", daemon=True,
                ).start()
        except DriveDisconnected:
            pass  # removal event resets the slot
        except HardwareError as exc:
            with self._lock:
                if self._check_gen.get(slot_id) != gen:
                    return
                slot.status = SlotStatus.ERROR
                slot.message = f"Health check error: {exc}"
        except Exception as exc:
            log.exception("Identify stalled on %s", slot_id)
            with self._lock:
                if self._check_gen.get(slot_id) != gen:
                    return
                slot.status = SlotStatus.ERROR
                slot.message = f"Identify failed — reseat drive ({exc})"
        finally:
            # Never leave the tile spinning CHECKING forever.
            with self._lock:
                if (self._check_gen.get(slot_id) == gen
                        and slot.status == SlotStatus.CHECKING):
                    slot.status = SlotStatus.ERROR
                    slot.message = "Identify stalled — reseat drive"

    # -- operator actions ----------------------------------------------------

    def confirm_wipe(self, slot_id: str) -> None:
        """Operator pressed YES. Large NVMe on a grading dock is queued to a
        wipe-only bay instead of starting dd here — unless force_local."""
        self._start_or_queue_wipe(slot_id, force_local=False)

    def confirm_wipe_here(self, slot_id: str) -> None:
        """Force wipe on this bay even if the drive is ≥1TB NVMe."""
        self._start_or_queue_wipe(slot_id, force_local=True)

    def queue_wipe(self, slot_id: str) -> None:
        """Mark this serial for wipe on a WIPE ONLY dock; do not destroy here."""
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                raise ValueError(f"Unknown slot {slot_id!r}")
            if slot.wipe_only:
                raise ValueError(f"{slot_id} is already a wipe-only bay")
            if slot.status != SlotStatus.READY or not slot.awaiting_confirm:
                raise ValueError(f"{slot_id} is not awaiting confirmation")
            if slot.drive is None:
                raise ValueError(f"{slot_id} has no bound drive identity")
            serial = slot.drive.serial
            self._queue_serial(serial, slot_id)
            slot.awaiting_confirm = False
            slot.message = f"Queued for WIPE ONLY dock — remove and insert there"
            log.info("Queued wipe for serial %s from %s", serial, slot_id)

    def _start_or_queue_wipe(self, slot_id: str, force_local: bool) -> None:
        with self._lock:
            slot = self.slots.get(slot_id)
            if slot is None:
                raise ValueError(f"Unknown slot {slot_id!r}")
            if slot.status != SlotStatus.READY or not slot.awaiting_confirm:
                raise ValueError(f"{slot_id} is not awaiting wipe confirmation")
            if slot.drive is None:
                raise ValueError(f"{slot_id} has no bound drive identity")
            bound = slot.drive
            auto_queue = (
                not force_local
                and not slot.wipe_only
                and bound.drive_type == DriveType.NVME
                and bound.capacity_bytes >= LARGE_NVME_QUEUE_BYTES
            )
            if auto_queue:
                self._queue_serial(bound.serial, slot_id)
                slot.awaiting_confirm = False
                slot.message = (
                    "≥1TB queued for WIPE ONLY dock — remove and insert there"
                )
                log.info("Auto-queued large NVMe %s from %s",
                         bound.serial, slot_id)
                return
            slot.awaiting_confirm = False
        threading.Thread(target=self._wipe_job, args=(slot_id, bound),
                         name=f"wipe-{slot_id}", daemon=True).start()

    def decline_wipe(self, slot_id: str) -> None:
        """Operator pressed NO/DONE. Clears a pending queue entry for this serial."""
        with self._lock:
            slot = self.slots[slot_id]
            if slot.status == SlotStatus.READY:
                if slot.drive is not None:
                    self._dequeue_serial(slot.drive.serial)
                slot.awaiting_confirm = False
                slot.queued_from = None
                if slot.prior_wipe and slot.prior_wipe.get("still_clean"):
                    slot.message = "Prior wipe still good — remove drive"
                else:
                    slot.message = "Not wiped — remove drive"

    def pending_wipes(self) -> list[dict]:
        with self._lock:
            return [
                {"serial": serial, "from_slot": from_slot}
                for serial, from_slot in self._pending_wipes.items()
            ]

    # -- wipe engine ---------------------------------------------------------

    def _fail(self, slot: SlotState, message: str,
              job_id: Optional[int], error: str,
              status: SlotStatus = SlotStatus.FAILED) -> None:
        # Log first, then update the screen: a result must never be visible
        # to the operator before it is durably recorded.
        if job_id is not None:
            self.joblog.finish_job(job_id, "FAILED", error)
        with self._lock:
            # One queue entry = one wipe attempt. Failures need a manual retry
            # (or re-queue) so service restarts don't loop auto-wipes.
            if slot.drive is not None:
                self._dequeue_serial(slot.drive.serial)
            slot.queued_from = None
            slot.status = status
            slot.message = message
            slot.wipe_eta_s = None

    def _set_phase_progress(self, slot: SlotState, frac: float, t0: float,
                            capacity_bytes: int) -> None:
        elapsed = max(0.0, time.monotonic() - t0)
        eta: Optional[float] = None
        bps: Optional[float] = None
        if frac > 0.02 and elapsed >= 0.75 and capacity_bytes > 0:
            done = frac * capacity_bytes
            bps = done / elapsed
            if frac < 0.999 and bps > 0:
                eta = (1.0 - frac) * capacity_bytes / bps
        with self._lock:
            slot.progress = frac
            slot.wipe_elapsed_s = elapsed
            slot.wipe_eta_s = eta
            slot.wipe_bps = bps

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
            if current.serial.casefold() != bound.serial.casefold():
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

            cap = bound.capacity_bytes or 0
            with self._lock:
                slot.status = SlotStatus.WIPING
                slot.wipe_method = method
                slot.progress = 0.0
                slot.wipe_elapsed_s = 0.0
                slot.wipe_eta_s = None
                slot.wipe_bps = None
                slot.message = ""

            wipe_t0 = time.monotonic()

            def wipe_progress(frac: float) -> None:
                self._set_phase_progress(slot, frac, wipe_t0, cap)

            self.backend.wipe(slot_id, method, wipe_progress)

            with self._lock:
                if slot.status != SlotStatus.WIPING:
                    # Removal event won the race; treat as disconnect.
                    raise DriveDisconnected("state changed during wipe")
                slot.status = SlotStatus.VERIFYING
                slot.progress = 0.0
                slot.wipe_elapsed_s = 0.0
                slot.wipe_eta_s = None
                slot.wipe_bps = None

            verify_t0 = time.monotonic()

            def verify_progress(frac: float) -> None:
                self._set_phase_progress(slot, frac, verify_t0, cap)

            self.backend.verify(slot_id, method, verify_progress)

            # SAFETY/AUDIT GATE 3: the same drive must still be present after
            # verification for the job to be recorded as a success.
            after = self.backend.read_identity(slot_id)
            if (after is None
                    or after.serial.casefold() != bound.serial.casefold()):
                raise WipeError("drive identity changed during job")

            # Log first, then update the screen (never show PASSED before the
            # record is durably committed).
            self.joblog.finish_job(job_id, "PASSED")
            with self._lock:
                self._dequeue_serial(bound.serial)
                slot.queued_from = None
                slot.status = SlotStatus.PASSED
                slot.progress = 1.0
                slot.wipe_eta_s = 0.0
                # Make success unmistakable on the board.
                method_label = (method.value if method else "WIPE").replace(
                    "_", " ")
                slot.message = (
                    f"WIPED & VERIFIED EMPTY ({method_label}) — "
                    f"safe to remove · /d/{bound.serial}"
                )
                slot.usage = UsageSnapshot(
                    capacity_bytes=bound.capacity_bytes,
                    used_bytes=0,
                    has_partitions=False,
                    label=f"Empty / {bound.capacity_label}",
                    detail="Post-wipe verification confirmed no residual data",
                )
                slot.prior_wipe = {
                    "result": "PASSED",
                    "finished_at": None,
                    "method": method.value if method else None,
                    "slot": slot_id,
                    "still_clean": True,
                    "had_passed": True,
                    "last_result": "PASSED",
                }

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
