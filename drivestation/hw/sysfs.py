"""Block-device discovery and ID_PATH resolution (Linux)."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


RunCmd = Callable[[list[str]], tuple[int, str, str]]


def default_run_cmd(argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


@dataclass
class BlockDevice:
    name: str          # e.g. sdc
    path: str          # e.g. /dev/sdc
    size_bytes: int
    id_path: str


def _is_os_disk(dev_path: str, run: RunCmd) -> bool:
    """True if this disk (or a partition on it) is mounted at / or /boot."""
    code, out, _ = run(["lsblk", "-J", "-o", "NAME,PKNAME,MOUNTPOINT", dev_path])
    if code != 0:
        # Fail closed for the candidate only when we cannot tell — treat as OS
        # if findmnt says so.
        pass
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        data = {}
    for node in data.get("blockdevices", []):
        if node.get("mountpoint") in ("/", "/boot", "/boot/efi"):
            return True
        for child in node.get("children") or []:
            if child.get("mountpoint") in ("/", "/boot", "/boot/efi"):
                return True

    code, out, _ = run(["findmnt", "-n", "-o", "SOURCE", "/"])
    if code == 0 and out.strip():
        src = out.strip()
        # /dev/sda2 → sda
        base = Path(src).name
        while base and base[-1].isdigit():
            # strip partition digits carefully: nvme0n1p2 → nvme0n1
            if "nvme" in base and "p" in base:
                base = base.rsplit("p", 1)[0]
                break
            base = base.rstrip("0123456789")
        disk_name = Path(dev_path).name
        if base == disk_name or src.startswith(dev_path):
            return True
    return False


def udevadm_id_path(dev_path: str, run: RunCmd = default_run_cmd) -> Optional[str]:
    code, out, _ = run(["udevadm", "info", "--query=property", f"--name={dev_path}"])
    if code != 0:
        return None
    for line in out.splitlines():
        if line.startswith("ID_PATH="):
            return line.split("=", 1)[1].strip()
    return None


def list_block_disks(run: RunCmd = default_run_cmd) -> list[BlockDevice]:
    """All disk-type block devices with size and ID_PATH (when available)."""
    code, out, _ = run(["lsblk", "-dbnJo", "NAME,TYPE,SIZE"])
    if code != 0:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []

    devices: list[BlockDevice] = []
    for node in data.get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        name = node.get("name")
        if not name:
            continue
        path = f"/dev/{name}"
        try:
            size = int(node.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        id_path = udevadm_id_path(path, run) or ""
        devices.append(BlockDevice(name=name, path=path, size_bytes=size, id_path=id_path))
    return devices


def scan_allowlisted(
    path_to_slot: dict[str, str],
    run: RunCmd = default_run_cmd,
) -> dict[str, BlockDevice]:
    """Return slot_id → BlockDevice for usable (size>0) allowlisted disks."""
    present: dict[str, BlockDevice] = {}
    for dev in list_block_disks(run):
        if not dev.id_path or dev.id_path not in path_to_slot:
            continue
        if dev.size_bytes <= 0:
            continue
        if _is_os_disk(dev.path, run):
            continue
        slot_id = path_to_slot[dev.id_path]
        present[slot_id] = dev
    return present
