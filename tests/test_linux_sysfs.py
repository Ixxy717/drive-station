import json

from drivestation.hw.slots_config import load_slots_config, path_to_slot
from drivestation.hw.sysfs import id_path_from_devpath, scan_allowlisted


def _make_run(devices: list[dict]):
    """devices: {name, size, id_path, mount?}"""

    by_name = {d["name"]: d for d in devices}

    def run(argv: list[str]):
        cmd = argv[0]
        if cmd == "lsblk" and "-J" in argv and "MOUNTPOINT" in "".join(argv):
            # OS disk check
            return 0, json.dumps({"blockdevices": []}), ""
        if cmd == "lsblk" and "-dbnJo" in argv:
            nodes = [
                {"name": d["name"], "type": "disk", "size": d["size"]}
                for d in devices
            ]
            return 0, json.dumps({"blockdevices": nodes}), ""
        if cmd == "lsblk" and "-dbno" in argv and "SIZE" in argv:
            path = argv[-1]
            name = path.rsplit("/", 1)[-1]
            return 0, str(by_name.get(name, {}).get("size", 0)), ""
        if cmd == "udevadm":
            path = None
            for a in argv:
                if a.startswith("--name="):
                    path = a.split("=", 1)[1]
            name = (path or "").rsplit("/", 1)[-1]
            id_path = by_name.get(name, {}).get("id_path", "")
            return 0, f"ID_PATH={id_path}\n", ""
        if cmd == "findmnt":
            return 1, "", ""
        return 1, "", f"unhandled {argv}"

    return run


def test_scan_maps_sata_luns_and_ignores_unknown():
    slots = load_slots_config()
    pmap = path_to_slot(slots)
    run = _make_run([
        {"name": "sda", "size": 500_000_000_000,
         "id_path": "pci-0000:00:17.0-ata-1"},  # OS-ish, not allowlisted
        {"name": "sdc", "size": 251_000_193_024,
         "id_path": slots["SATA-1"].id_path},
        {"name": "sde", "size": 500_107_862_016,
         "id_path": slots["SATA-2"].id_path},
        {"name": "sdf", "size": 16_000_000_000,
         "id_path": "pci-usb-thumb-drive"},
        {"name": "sdd", "size": 0,
         "id_path": slots["NVME-A1"].id_path},  # ghost
    ])
    present = scan_allowlisted(pmap, run)
    assert set(present) == {"SATA-1", "SATA-2"}
    assert present["SATA-1"].path == "/dev/sdc"
    assert present["SATA-2"].path == "/dev/sde"


def test_ghost_zero_size_excluded():
    slots = load_slots_config()
    pmap = path_to_slot(slots)
    run = _make_run([
        {"name": "sdd", "size": 0, "id_path": slots["NVME-A1"].id_path},
    ])
    assert scan_allowlisted(pmap, run) == {}


def test_id_path_synthesized_from_sysfs_when_udev_blank():
    """Sabrent on USB3 sometimes has no ID_PATH props — derive from DEVPATH."""
    path = (
        "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-4/2-4.4/2-4.4.4/"
        "2-4.4.4.4/2-4.4.4.4:1.0/host8/target8:0:0/8:0:0:0/block/sdd"
    )
    assert id_path_from_devpath(path) == (
        "pci-0000:00:14.0-usb-0:4.4.4.4:1.0-scsi-0:0:0:0"
    )
    lun1 = path.replace("8:0:0:0", "8:0:0:1")
    assert id_path_from_devpath(lun1) == (
        "pci-0000:00:14.0-usb-0:4.4.4.4:1.0-scsi-0:0:0:1"
    )
