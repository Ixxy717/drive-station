"""Best-effort pre-wipe used-space probe (filesystem mount or signature scan)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

from .sysfs import RunCmd, default_run_cmd


@dataclass(frozen=True)
class UsageInfo:
    """How much looks occupied before a wipe."""
    capacity_bytes: int
    used_bytes: Optional[int]  # None when we cannot measure
    has_partitions: bool
    label: str                 # short UI string
    detail: str = ""           # longer note for flags / listing page

    def to_dict(self) -> dict:
        return {
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "has_partitions": self.has_partitions,
            "label": self.label,
            "detail": self.detail,
        }


def _fmt_bytes(n: int) -> str:
    gb = n / 1_000_000_000
    if gb >= 1000:
        tb = gb / 1000
        return f"{tb:.0f}TB" if tb == int(tb) else f"{tb:.1f}TB"
    if gb >= 10:
        return f"{gb:.0f}GB"
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = n / 1_000_000
    return f"{mb:.0f}MB"


def _disk_size(dev_path: str, run: RunCmd) -> int:
    code, out, _ = run(["lsblk", "-dbno", "SIZE", dev_path])
    if code != 0 or not out.strip():
        return 0
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def _lsblk_tree(dev_path: str, run: RunCmd) -> list[dict]:
    code, out, _ = run([
        "lsblk", "-Jb", "-o",
        "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,FSUSED",
        dev_path,
    ])
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return list(data.get("blockdevices") or [])


def _walk_parts(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for n in nodes:
        if n.get("type") in ("part", "crypt"):
            out.append(n)
        kids = n.get("children") or []
        if kids:
            out.extend(_walk_parts(kids))
    return out


def _mount_fsused(part_path: str, fstype: str, run: RunCmd) -> Optional[int]:
    """Read-only mount briefly; return used bytes or None."""
    if not part_path or not fstype:
        return None
    # Skip things that will never mount cleanly here
    if fstype.lower() in ("crypto_luks", "bitlocker", "swap", "LVM2_member"):
        return None
    mnt = tempfile.mkdtemp(prefix="ds-mnt-")
    try:
        code, _, err = run([
            "mount", "-o", "ro,noload,noexec,nosuid,nodev",
            "-t", fstype, part_path, mnt,
        ])
        if code != 0:
            # Generic mount (let mount guess type)
            code, _, err = run([
                "mount", "-o", "ro,noexec,nosuid,nodev", part_path, mnt,
            ])
        if code != 0:
            return None
        try:
            st = os.statvfs(mnt)
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            return int(used)
        finally:
            run(["umount", "-l", mnt])
    except OSError:
        return None
    finally:
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def _has_payload_signatures(dev_path: str) -> Optional[bool]:
    """True/False if MBR/GPT/FS magic seen; None if media is unreadable (EIO)."""
    try:
        fd = os.open(dev_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        # ATA-locked / failing media often returns EIO on raw reads —
        # never abort identify for that.
        head = os.read(fd, 2 * 1024 * 1024)
    except OSError:
        return None
    finally:
        os.close(fd)
    if not head:
        return False
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        # Protective/real MBR — treat non-empty partition type bytes as data
        parts = head[446:510]
        if any(parts[i + 4] != 0 for i in range(0, 64, 16)):
            return True
    if b"EFI PART" in head[:1024 * 1024]:
        return True
    for magic in (b"NTFS    ", b"EXFAT   ", b"\x53\xef",  # ext*
                  b"XFSB", b"HV\xd1\xd1", b"RRaA"):
        if magic in head:
            return True
    # Any substantial non-zero content in first 64KiB (beyond a blank disk)
    sample = head[:65536]
    nonzero = sum(1 for b in sample if b != 0)
    return nonzero > 64


def read_usage(
    dev_path: str,
    capacity_bytes: int = 0,
    run: RunCmd = default_run_cmd,
) -> UsageInfo:
    cap = capacity_bytes or _disk_size(dev_path, run)
    tree = _lsblk_tree(dev_path, run)
    parts = _walk_parts(tree)

    used_total = 0
    measured = 0
    blocked = 0
    for p in parts:
        path = p.get("path") or (f"/dev/{p['name']}" if p.get("name") else "")
        fstype = (p.get("fstype") or "").strip()
        fsused = p.get("fsused")
        if fsused not in (None, "", "null"):
            try:
                used_total += int(fsused)
                measured += 1
                continue
            except (TypeError, ValueError):
                pass
        if not fstype:
            continue
        if fstype.lower() in ("crypto_luks", "bitlocker"):
            blocked += 1
            continue
        got = _mount_fsused(path, fstype, run)
        if got is not None:
            used_total += got
            measured += 1
        else:
            blocked += 1

    has_parts = bool(parts)
    if measured:
        label = f"{_fmt_bytes(used_total)} / {_fmt_bytes(cap)} used"
        detail = f"Measured from {measured} filesystem(s)"
        if blocked:
            detail += f"; {blocked} partition(s) unreadable"
        return UsageInfo(cap, used_total, has_parts, label, detail)

    if blocked or (has_parts and any((p.get("fstype") or "") for p in parts)):
        return UsageInfo(
            cap, None, True,
            f"Data present / {_fmt_bytes(cap)}",
            "Partitions found but used space could not be measured "
            "(encrypted or unsupported filesystem)",
        )

    if has_parts:
        return UsageInfo(
            cap, None, True,
            f"Partitions / {_fmt_bytes(cap)}",
            "Partition table present; no mounted filesystem usage available",
        )

    sig = _has_payload_signatures(dev_path)
    if sig is None:
        return UsageInfo(
            cap, None, False,
            f"Unreadable / {_fmt_bytes(cap)}",
            "Raw read failed — drive may be ATA-locked or media error",
        )
    if sig:
        return UsageInfo(
            cap, None, False,
            f"Data present / {_fmt_bytes(cap)}",
            "No partitions reported, but disk signature/data detected",
        )

    return UsageInfo(
        cap, 0, False,
        f"Empty / {_fmt_bytes(cap)}",
        "No partitions or filesystem signatures detected",
    )
