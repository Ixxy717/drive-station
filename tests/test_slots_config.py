from pathlib import Path

import pytest

from drivestation.hw.slots_config import SlotsConfigError, load_slots_config
from drivestation.models import SLOT_LAYOUT


def test_default_slots_toml_loads_and_matches_layout():
    slots = load_slots_config()
    assert set(slots) == {s for s, _ in SLOT_LAYOUT}
    # StarTech NVMe toasters mapped.
    assert slots["NVME-A1"].id_path.endswith("usb-0:4.4.4.1:1.0-scsi-0:0:0:0")
    assert slots["NVME-B1"].id_path.endswith("usb-0:4.4.4.2:1.0-scsi-0:0:0:0")
    assert slots["NVME-A1"].bridge == "asm2362"
    assert slots["NVME-B1"].bridge == "asm2362"
    assert slots["NVME-A1"].hot_swap is True
    # StarTech 4-bay SATA — hot-swap; 2026-08-07 --quad map.
    for sid in ("SATA-1", "SATA-2", "SATA-3", "SATA-4"):
        assert slots[sid].bridge == "asmedia_sata"
        assert slots[sid].hot_swap is True
        assert slots[sid].shared_power_group == "STARTECH SATA"
        assert "usb-0:8.4.4.4." in slots[sid].id_path
    assert slots["SATA-1"].id_path.endswith("usb-0:8.4.4.4.3:1.0-scsi-0:0:0:0")
    assert slots["SATA-2"].id_path.endswith("usb-0:8.4.4.4.4:1.0-scsi-0:0:0:0")
    assert slots["SATA-3"].id_path.endswith("usb-0:8.4.4.4.2:1.0-scsi-0:0:0:0")
    assert slots["SATA-4"].id_path.endswith("usb-0:8.4.4.4.1:1.0-scsi-0:0:0:0")
    # SUITOK wipe-only (both duals on hub port 4.4.x)
    assert slots["SUITOK-1"].bridge == "rtl9210"
    assert slots["SUITOK-1"].id_path.endswith("usb-0:4.4.3.1:1.0-scsi-0:0:0:0")
    assert slots["SUITOK-2"].id_path.endswith("usb-0:4.4.3.2:1.0-scsi-0:0:0:0")
    assert slots["SUITOK-3"].id_path.endswith("usb-0:4.4.2.1:1.0-scsi-0:0:0:0")
    assert slots["SUITOK-4"].id_path.endswith("usb-0:4.4.2.2:1.0-scsi-0:0:0:0")
    assert slots["M2-1"].bridge == "rtl9220"
    assert "QUAD-1" not in slots


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(SlotsConfigError, match="not found"):
        load_slots_config(tmp_path / "nope.toml")


def test_mismatch_raises(tmp_path: Path):
    p = tmp_path / "slots.toml"
    p.write_text("""
[slots.SATA-1]
id_path = "pci-x"
bridge = "asmedia_sata"
hot_swap = true
""", encoding="utf-8")
    with pytest.raises(SlotsConfigError, match="mismatch"):
        load_slots_config(p)


def test_duplicate_path_raises(tmp_path: Path):
    lines = ['[slots.SATA-1]', 'id_path = "same"', 'bridge = "asmedia_sata"',
             'hot_swap = true', '[slots.SATA-2]', 'id_path = "same"',
             'bridge = "asmedia_sata"', 'hot_swap = true']
    # fill remaining required slots with unique paths
    for i, (sid, _) in enumerate(SLOT_LAYOUT):
        if sid in ("SATA-1", "SATA-2"):
            continue
        lines += [f"[slots.{sid}]", f'id_path = "path-{i}"',
                  'bridge = "rtl9210"', "hot_swap = true"]
    p = tmp_path / "slots.toml"
    p.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(SlotsConfigError, match="duplicate"):
        load_slots_config(p)
