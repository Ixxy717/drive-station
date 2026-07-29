from pathlib import Path

import pytest

from drivestation.hw.slots_config import SlotsConfigError, load_slots_config
from drivestation.models import SLOT_LAYOUT


def test_default_slots_toml_loads_and_matches_layout():
    slots = load_slots_config()
    assert set(slots) == {s for s, _ in SLOT_LAYOUT}
    assert slots["SATA-1"].id_path.endswith("scsi-0:0:0:0")
    assert slots["SATA-2"].id_path.endswith("scsi-0:0:0:1")
    assert slots["SATA-1"].bridge == "asmedia_sata"
    assert slots["NVME-A1"].bridge == "rtl9210"
    assert slots["M2-1"].bridge == "rtl9220"
    assert slots["SATA-1"].hot_swap is False
    assert slots["NVME-A1"].hot_swap is True


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(SlotsConfigError, match="not found"):
        load_slots_config(tmp_path / "nope.toml")


def test_mismatch_raises(tmp_path: Path):
    p = tmp_path / "slots.toml"
    p.write_text("""
[slots.SATA-1]
id_path = "pci-x"
bridge = "asmedia_sata"
hot_swap = false
""", encoding="utf-8")
    with pytest.raises(SlotsConfigError, match="mismatch"):
        load_slots_config(p)


def test_duplicate_path_raises(tmp_path: Path):
    lines = ['[slots.SATA-1]', 'id_path = "same"', 'bridge = "asmedia_sata"',
             'hot_swap = false', '[slots.SATA-2]', 'id_path = "same"',
             'bridge = "asmedia_sata"', 'hot_swap = false']
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
