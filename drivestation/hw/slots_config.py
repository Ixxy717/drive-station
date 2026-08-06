"""Load and validate config/slots.toml against SLOT_LAYOUT."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..models import SLOT_LAYOUT

DEFAULT_SLOTS_PATH = Path(__file__).resolve().parents[2] / "config" / "slots.toml"

# asm2362  — ASMedia ASM2362 NVMe bridge (StarTech 1USB3-NVME-DOCK, ACASIS M03);
#            NVMe SMART via `smartctl -d sntasmedia`.
# sas_usb  — USB SAS/SATA enclosure (Maiwo K3016S etc); SAS drives answer
#            `smartctl -d scsi`, SATA drives behind it answer `-d sat`.
VALID_BRIDGES = frozenset(
    {"asmedia_sata", "rtl9210", "rtl9220", "asm2362", "sas_usb"})


@dataclass(frozen=True)
class SlotConfig:
    slot_id: str
    id_path: str
    bridge: str
    hot_swap: bool
    shared_power_group: Optional[str] = None


class SlotsConfigError(Exception):
    """slots.toml is missing, invalid, or does not match SLOT_LAYOUT."""


def load_slots_config(path: Optional[Path] = None) -> dict[str, SlotConfig]:
    cfg_path = Path(path) if path is not None else DEFAULT_SLOTS_PATH
    if not cfg_path.is_file():
        raise SlotsConfigError(
            f"Slot config not found: {cfg_path}. "
            "Real mode requires config/slots.toml (or DRIVESTATION_SLOTS)."
        )

    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, dict) or not slots_raw:
        raise SlotsConfigError(f"{cfg_path}: missing [slots.*] tables")

    expected = {slot_id for slot_id, _ in SLOT_LAYOUT}
    found = set(slots_raw.keys())
    if found != expected:
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"extra {extra}")
        raise SlotsConfigError(f"{cfg_path}: SLOT_LAYOUT mismatch ({'; '.join(parts)})")

    by_path: dict[str, str] = {}
    result: dict[str, SlotConfig] = {}
    for slot_id, body in slots_raw.items():
        if not isinstance(body, dict):
            raise SlotsConfigError(f"{cfg_path}: [slots.{slot_id}] must be a table")
        id_path = body.get("id_path")
        bridge = body.get("bridge")
        if not id_path or not isinstance(id_path, str):
            raise SlotsConfigError(f"{cfg_path}: slots.{slot_id}.id_path required")
        if bridge not in VALID_BRIDGES:
            raise SlotsConfigError(
                f"{cfg_path}: slots.{slot_id}.bridge must be one of {sorted(VALID_BRIDGES)}"
            )
        if id_path in by_path:
            raise SlotsConfigError(
                f"{cfg_path}: duplicate id_path for {by_path[id_path]} and {slot_id}"
            )
        by_path[id_path] = slot_id
        result[slot_id] = SlotConfig(
            slot_id=slot_id,
            id_path=id_path,
            bridge=str(bridge),
            hot_swap=bool(body.get("hot_swap", True)),
            shared_power_group=body.get("shared_power_group"),
        )
    return result


def path_to_slot(slots: dict[str, SlotConfig]) -> dict[str, str]:
    return {cfg.id_path: cfg.slot_id for cfg in slots.values()}
