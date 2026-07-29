"""Pre-wipe usage labels + post-wipe signature checks."""
from pathlib import Path

from drivestation.hw.usage import UsageInfo, _fmt_bytes, read_usage
from drivestation.hw.wipe_linux import verify_no_filesystem
from drivestation.hw.base import VerifyError
import pytest


def test_fmt_bytes():
    assert _fmt_bytes(256_000_000_000) == "256GB"
    assert _fmt_bytes(2_000_000_000_000) == "2TB"


def test_read_usage_empty_disk(tmp_path: Path):
    disk = tmp_path / "disk.bin"
    disk.write_bytes(b"\x00" * (2 * 1024 * 1024))

    def run(argv):
        if argv[:2] == ["lsblk", "-dbno"]:
            return 0, str(disk.stat().st_size), ""
        if argv[0] == "lsblk" and "-Jb" in argv:
            size = disk.stat().st_size
            payload = (
                '{"blockdevices":[{"name":"disk","type":"disk","size":'
                + str(size)
                + ',"children":[]}]}'
            )
            return 0, payload, ""
        return 1, "", "skip"

    info = read_usage(str(disk), capacity_bytes=disk.stat().st_size, run=run)
    assert info.used_bytes == 0
    assert "Empty" in info.label


def test_read_usage_sums_mounted_fsused():
    def run(argv):
        if argv[:2] == ["lsblk", "-dbno"]:
            return 0, "256000000000", ""
        if argv[0] == "lsblk" and "-Jb" in argv:
            return 0, """{
              "blockdevices": [{
                "name": "sda", "type": "disk", "size": 256000000000,
                "children": [{
                  "name": "sda1", "path": "/dev/sda1", "type": "part",
                  "size": 256000000000, "fstype": "ntfs",
                  "mountpoint": "/mnt/x", "fsused": 60000000000
                }]
              }]
            }""", ""
        return 1, "", ""

    info = read_usage("/dev/sda", capacity_bytes=256_000_000_000, run=run)
    assert info.used_bytes == 60_000_000_000
    assert "60GB" in info.label
    assert "256GB" in info.label


def test_verify_no_filesystem_rejects_gpt(tmp_path: Path, monkeypatch):
    disk = tmp_path / "gpt.bin"
    blob = bytearray(2 * 1024 * 1024)
    blob[512:520] = b"EFI PART"
    disk.write_bytes(blob)

    monkeypatch.setattr(
        "drivestation.hw.wipe_linux._device_size_bytes",
        lambda path, run: len(blob),
    )
    with pytest.raises(VerifyError, match="filesystem signature"):
        verify_no_filesystem(str(disk), run=lambda a: (0, "", ""))


def test_usage_snapshot_dict():
    u = UsageInfo(100, 40, True, "40GB / 100GB used", "ok")
    d = u.to_dict()
    assert d["used_bytes"] == 40
    assert d["label"].startswith("40GB")
