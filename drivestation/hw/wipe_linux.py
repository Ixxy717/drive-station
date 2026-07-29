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

    # Use dd with status=progress; parse bytes written from stderr.
    argv = [
        "dd", f"if=/dev/zero", f"of={dev_path}",
        "bs=16M", "conv=fsync", "status=progress",
    ]
    import subprocess
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as exc:
        raise WipeError("dd not found") from exc

    last_frac = 0.0
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
            written = int(m.group(1))
            frac = min(0.99, written / size)
            if frac >= last_frac:
                last_frac = frac
                progress(frac)

    rc = proc.wait()
    if not present():
        raise DriveDisconnected(f"{dev_path} disappeared during overwrite")
    if rc != 0:
        raise WipeError(f"dd failed (exit {rc}): {''.join(stderr_data)[-500:]}")
    progress(1.0)


def verify_zeros(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
    samples: int = 6,
    chunk: int = 1024 * 1024,
) -> None:
    """Read sample regions; all bytes must be zero."""
    size = _device_size_bytes(dev_path, run)
    if size <= 0:
        raise VerifyError(f"cannot size {dev_path} for verify")
    if not _device_present(dev_path):
        raise DriveDisconnected(f"{dev_path} missing during verify")

    offsets = [0]
    if size > chunk:
        offsets.append(max(0, size // 2 - chunk // 2))
        offsets.append(max(0, size - chunk))
    # a few deterministic mid points
    for i in range(samples - len(offsets)):
        offsets.append(int(size * (i + 1) / (samples + 1)))

    try:
        fd = os.open(dev_path, os.O_RDONLY)
    except OSError as exc:
        raise VerifyError(f"open failed: {exc}") from exc

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
            progress((i + 1) / len(offsets))
    finally:
        os.close(fd)
    progress(1.0)


def ata_enhanced_erase(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
    poll_present: Optional[PresentFn] = None,
) -> str:
    """
    Run ATA SECURITY ERASE ENHANCED.
    Returns verify_mode label for logging: 'ata_erase_status'.
    """
    present = poll_present or (lambda: _device_present(dev_path))
    if not present():
        raise DriveDisconnected(f"{dev_path} missing before erase")

    state = ata_security_state(dev_path, run)
    if state["frozen"]:
        raise WipeError("ATA security is frozen — power-cycle the dock and retry")
    if not state["enhanced_erase"]:
        raise WipeError("ATA enhanced erase not supported on this drive")

    unmount_disk(dev_path, run)
    progress(0.05)

    code, out, err = run([
        "hdparm", "--user-master", "u",
        "--security-set-pass", ATA_PASSWORD, dev_path,
    ])
    if code != 0:
        raise WipeError(f"security-set-pass failed: {err or out}")

    progress(0.1)
    # Long-running; use Popen so we can poll presence.
    import subprocess
    argv = [
        "hdparm", "--user-master", "u",
        "--security-erase-enhanced", ATA_PASSWORD, dev_path,
    ]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise WipeError("hdparm not found") from exc

    # Pulse progress while waiting (no real %).
    t0 = time.monotonic()
    while proc.poll() is None:
        if not present():
            proc.kill()
            raise DriveDisconnected(f"{dev_path} disappeared during ATA erase")
        elapsed = time.monotonic() - t0
        # Asymptotic crawl toward 0.95
        progress(min(0.95, 0.1 + elapsed / (elapsed + 120) * 0.85))
        time.sleep(1.0)

    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        # Try to unlock
        run([
            "hdparm", "--user-master", "u",
            "--security-disable", ATA_PASSWORD, dev_path,
        ])
        raise WipeError(
            f"security-erase-enhanced failed: {stderr or stdout}"
        )

    if not present():
        raise DriveDisconnected(f"{dev_path} missing after ATA erase")

    progress(1.0)
    return "ata_erase_status"


def verify_ata_erase(
    dev_path: str,
    progress: ProgressCallback,
    run: RunCmd = default_run_cmd,
) -> None:
    """ATA erase verify: device responds (zeros not required). Serial check
    is enforced by Station after verify via read_identity."""
    progress(0.3)
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
    progress(0.6)

    try:
        fd = os.open(dev_path, os.O_RDONLY)
        try:
            os.read(fd, 4096)
        finally:
            os.close(fd)
    except OSError as exc:
        raise VerifyError(f"device not readable after erase: {exc}") from exc
    progress(1.0)
