"""Destructive wipe + verify for Linux docks."""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Optional

from .base import DriveDisconnected, ProgressCallback, VerifyError, WipeError
from .identify import ata_security_state
from .sysfs import RunCmd, default_run_cmd

log = logging.getLogger("drivestation.wipe")

ATA_PASSWORD = "DrvStn"
PresentFn = Callable[[], bool]


def _device_present(dev_path: str) -> bool:
    try:
        return os.path.exists(dev_path)
    except OSError:
        return False


def _device_size_bytes(dev_path: str, run: RunCmd) -> int:
    code, out, _ = run(["lsblk", "-dbno", "SIZE", dev_path])
    if code != 0:
        return 0
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def unmount_disk(dev_path: str, run: RunCmd = default_run_cmd) -> None:
    """Unmount any mounted partitions on this disk (not the OS disk)."""
    code, out, _ = run(["lsblk", "-lnpo", "NAME,MOUNTPOINT", dev_path])
    if code != 0:
        return
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        node, mp = parts[0], parts[1].strip()
        if not mp or mp in ("/", "/boot", "/boot/efi"):
            continue
        run(["umount", "-f", node])


def zero_overwrite(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
    poll_present: Optional[PresentFn] = None,
) -> None:
    """Write zeros across the whole device with progress."""
    present = poll_present or (lambda: _device_present(dev_path))
    if not present():
        raise DriveDisconnected(f"{dev_path} missing before overwrite")

    unmount_disk(dev_path, run)
    size = _device_size_bytes(dev_path, run)
    if size <= 0:
        raise WipeError(f"cannot determine size of {dev_path}")

    # Exact block count so dd usually exits 0. Still treat ENOSPC after a
    # full-size write as success — classic "filled the whole disk" dd quirk.
    bs = 16 * 1024 * 1024
    count = max(1, (size + bs - 1) // bs)
    argv = [
        "dd", "if=/dev/zero", f"of={dev_path}",
        f"bs={bs}", f"count={count}", "conv=fsync", "status=progress",
    ]
    import subprocess
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as exc:
        raise WipeError("dd not found") from exc

    last_frac = 0.0
    written = 0
    stderr_data = []
    assert proc.stderr is not None
    while True:
        if not present():
            proc.kill()
            raise DriveDisconnected(f"{dev_path} disappeared during overwrite")
        line = proc.stderr.readline()
        if not line and proc.poll() is not None:
            break
        if not line:
            time.sleep(0.05)
            continue
        stderr_data.append(line)
        # dd progress: "123456789 bytes (123 MB, 117 MiB) copied, ..."
        m = re.search(r"(\d+)\s+bytes", line.replace(",", ""))
        if m and size > 0:
            written = max(written, int(m.group(1)))
            frac = min(0.99, written / size)
            if frac >= last_frac:
                last_frac = frac
                progress(frac)

    rc = proc.wait()
    err_tail = "".join(stderr_data)[-800:]
    if rc != 0:
        filled = written >= size or (
            "No space left" in err_tail and written >= int(size * 0.999)
        )
        if filled:
            log.info(
                "dd exited %s after writing %s/%s bytes — treating as full wipe OK",
                rc, written, size,
            )
        else:
            # If the bridge blipped but we already wrote the whole disk,
            # still accept — verify will re-resolve the path.
            if written >= int(size * 0.999):
                log.warning(
                    "dd exited %s after ~full write (%s/%s); continuing to verify",
                    rc, written, size,
                )
            else:
                raise WipeError(f"dd failed (exit {rc}): {err_tail}")
    progress(1.0)
    # Give USB bridges a moment before verify opens the device again.
    # Do not require present() here — docks often drop for a few seconds
    # right after a full overwrite (that used to look like a yank).
    time.sleep(1.5)


_FS_MAGICS = (
    b"EFI PART",
    b"NTFS    ",
    b"EXFAT   ",
    b"XFSB",
    b"HV\xd1\xd1",
    b"RRaA",
    b"\x53\xef",  # ext superblock magic (little-endian at +0x438; also scanned raw)
)


def _read_slice(dev_path: str, offset: int, length: int) -> bytes:
    fd = os.open(dev_path, os.O_RDONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)
    finally:
        os.close(fd)


def verify_no_filesystem(
    dev_path: str,
    run: RunCmd = default_run_cmd,
) -> None:
    """Fail if partition table / common FS signatures remain after wipe."""
    size = _device_size_bytes(dev_path, run)
    if size <= 0:
        raise VerifyError(f"cannot size {dev_path} for verify")
    # Head + GPT backup near end + a couple mid samples
    regions = [0]
    if size > 2 * 1024 * 1024:
        regions.append(max(0, size - 2 * 1024 * 1024))
    regions.append(max(0, size // 3))
    regions.append(max(0, (2 * size) // 3))

    for off in regions:
        try:
            data = _read_slice(dev_path, off, 2 * 1024 * 1024)
        except OSError as exc:
            raise VerifyError(f"verify read failed at {off}: {exc}") from exc
        if not data:
            raise VerifyError(f"short verify read at offset {off}")
        if off == 0 and len(data) >= 512 and data[510:512] == b"\x55\xaa":
            parts = data[446:510]
            if any(parts[i + 4] != 0 for i in range(0, 64, 16)):
                raise VerifyError("MBR partition table still present after wipe")
        for magic in _FS_MAGICS:
            if magic in data:
                raise VerifyError(
                    f"filesystem signature {magic!r} still present after wipe"
                )


def verify_zeros(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
    samples: int = 12,
    chunk: int = 2 * 1024 * 1024,
) -> None:
    """Sample many regions — all must be zero — then confirm no FS signatures."""
    size = _device_size_bytes(dev_path, run)
    if size <= 0:
        raise VerifyError(f"cannot size {dev_path} for verify")
    if not _device_present(dev_path):
        raise DriveDisconnected(f"{dev_path} missing during verify")

    offsets = [0]
    if size > chunk:
        offsets.append(max(0, size // 2 - chunk // 2))
        offsets.append(max(0, size - chunk))
    for i in range(max(0, samples - len(offsets))):
        offsets.append(int(size * (i + 1) / (samples + 1)))
    # de-dupe while preserving order
    seen: set[int] = set()
    uniq = []
    for off in offsets:
        if off not in seen:
            seen.add(off)
            uniq.append(off)
    offsets = uniq

    fd = None
    last_open: Optional[OSError] = None
    for _ in range(5):
        try:
            fd = os.open(dev_path, os.O_RDONLY)
            last_open = None
            break
        except OSError as exc:
            last_open = exc
            # ENXIO/ENODEV right after a USB rewrite — brief settle + retry.
            if exc.errno in (5, 6, 19):
                time.sleep(0.8)
                continue
            raise VerifyError(f"open failed: {exc}") from exc
    if fd is None:
        assert last_open is not None
        if last_open.errno in (6, 19):
            raise DriveDisconnected(
                f"{dev_path} missing during verify: {last_open}")
        raise VerifyError(f"open failed: {last_open}") from last_open

    try:
        for i, off in enumerate(offsets):
            if not _device_present(dev_path):
                raise DriveDisconnected(f"{dev_path} missing during verify")
            length = min(chunk, max(0, size - off))
            if length <= 0:
                continue
            os.lseek(fd, off, os.SEEK_SET)
            data = os.read(fd, length)
            if not data:
                raise VerifyError(f"short read at offset {off}")
            if any(b != 0 for b in data):
                raise VerifyError(f"non-zero data at offset {off}")
            progress(0.9 * (i + 1) / len(offsets))
    finally:
        os.close(fd)

    verify_no_filesystem(dev_path, run)
    progress(1.0)


def _run_ata_erase_cmd(
    argv: list[str],
    progress: ProgressCallback,
    present: PresentFn,
) -> tuple[int, str, str]:
    """Run long-running hdparm erase; pulse progress; kill if yanked."""
    import subprocess
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise WipeError("hdparm not found") from exc

    t0 = time.monotonic()
    while proc.poll() is None:
        if not present():
            proc.kill()
            raise DriveDisconnected(f"device disappeared during ATA erase")
        elapsed = time.monotonic() - t0
        progress(min(0.95, 0.1 + elapsed / (elapsed + 120) * 0.85))
        time.sleep(1.0)
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout or "", stderr or ""


def ata_enhanced_erase(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
    poll_present: Optional[PresentFn] = None,
) -> str:
    """
    Run ATA SECURITY ERASE ENHANCED (or normal erase as fallback).

    Locked drives (unknown prior password): skip set-pass and try erase with
    NULL / empty / station password on user and master accounts — that is the
    only software path that can clear a locked volume without the old password.
    Returns verify_mode label for logging: 'ata_erase_status'.
    """
    present = poll_present or (lambda: _device_present(dev_path))
    if not present():
        raise DriveDisconnected(f"{dev_path} missing before erase")

    state = ata_security_state(dev_path, run)
    if state["frozen"]:
        raise WipeError("ATA security is frozen — power-cycle the dock and retry")
    enhanced = bool(state.get("enhanced_erase"))
    locked = bool(state.get("locked"))

    unmount_disk(dev_path, run)
    progress(0.05)

    erase_flags = (
        ("--security-erase-enhanced",) if enhanced
        else ("--security-erase",)
    )
    # Prefer enhanced; if locked attempts fail, also try normal erase.
    erase_variants: list[tuple[str, ...]] = [erase_flags]
    if enhanced:
        erase_variants.append(("--security-erase",))

    if locked:
        # Already password-locked — cannot set-pass. Try common empty passwords.
        attempts: list[tuple[str, str]] = []
        for pw in ("NULL", "", ATA_PASSWORD):
            for um in ("u", "m"):
                attempts.append((um, pw))
        last_err = "locked erase failed"
        for i, (um, pw) in enumerate(attempts):
            for flags in erase_variants:
                argv = [
                    "hdparm", "--user-master", um, *flags, pw, dev_path,
                ]
                log.info("ATA locked erase try: %s", " ".join(argv[:-1] + ["***"]))
                code, out, err = _run_ata_erase_cmd(argv, progress, present)
                if code == 0:
                    if not present():
                        raise DriveDisconnected(
                            f"{dev_path} missing after ATA erase")
                    progress(1.0)
                    return "ata_erase_status"
                last_err = err or out or f"exit {code}"
            progress(0.05 + 0.05 * (i + 1) / max(1, len(attempts)))
        raise WipeError(
            f"ATA LOCKED — erase bypass failed ({last_err}). "
            "Overwrite may also fail; scrap if I/O errors persist."
        )

    code, out, err = run([
        "hdparm", "--user-master", "u",
        "--security-set-pass", ATA_PASSWORD, dev_path,
    ])
    if code != 0:
        raise WipeError(f"security-set-pass failed: {err or out}")

    progress(0.1)
    argv = [
        "hdparm", "--user-master", "u",
        erase_flags[0], ATA_PASSWORD, dev_path,
    ]
    code, out, err = _run_ata_erase_cmd(argv, progress, present)
    if code != 0:
        run([
            "hdparm", "--user-master", "u",
            "--security-disable", ATA_PASSWORD, dev_path,
        ])
        raise WipeError(f"security erase failed: {err or out}")

    if not present():
        raise DriveDisconnected(f"{dev_path} missing after ATA erase")

    progress(1.0)
    return "ata_erase_status"


def verify_ata_erase(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
) -> None:
    """ATA erase verify: identity still readable + no FS/partition leftovers.

    Full-disk zero sampling is not required — some SSDs return non-zero
    after secure erase — but partition/FS signatures must be gone.
    """
    progress(0.2)
    code, out, _ = run(["smartctl", "-j", "-i", "-d", "sat", dev_path])
    if not out.strip():
        raise VerifyError("identity unreadable after ATA erase")
    import json
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise VerifyError("identity unreadable after ATA erase") from exc
    if not (data.get("serial_number") or "").strip():
        raise VerifyError("serial missing after ATA erase")
    progress(0.5)

    try:
        fd = os.open(dev_path, os.O_RDONLY)
        try:
            os.read(fd, 4096)
        finally:
            os.close(fd)
    except OSError as exc:
        raise VerifyError(f"device not readable after erase: {exc}") from exc

    progress(0.7)
    verify_no_filesystem(dev_path, run)
    progress(1.0)
