# drive-station

Drive testing and secure-wiping station for an electronics recycling / ITAD bench.

One mini PC + USB docks (2× SATA bays, 4× NVMe M.2 slots, 1× M.2 NVMe/SATA slot)
= a 7-slot appliance. Operators insert drives, look at the screen, click YES/NO,
and remove finished drives. Everything else is automatic.

## How it works

- **Slot mapping**: every physical dock slot has a permanent name (`SATA-1`,
  `NVME-A1`, `M2-1`, ...). On Linux, slots are identified by the USB `ID_PATH`
  (including SCSI LUN for the dual SATA dock), never by `/dev/sdX` names.
  Devices not listed in `config/slots.toml` are ignored.
- **Simulator mode**: on Windows (or anywhere without the real docks) the app
  runs against fake docks and fake drives, including injectable faults.
- **Safety**: wipes are bound to slot + serial. The serial is re-read and
  re-verified immediately before any destructive command. The OS drive and
  non-allowlisted USB devices are structurally unreachable.

## Running (development, simulator mode)

```
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8330 — the kiosk board plus a simulator control panel.

## Tests

```
python -m pytest
```

## Deploying to the station mini PC (Linux)

**Do not move the dock USB cables** after characterization — paths in
`config/slots.toml` are frozen to those ports. If you must re-cable, re-run
`tools/dock_characterize.sh` / `--dual` and update the file.

```
git clone https://github.com/Ixxy717/drive-station.git
cd drive-station
sudo apt install smartmontools nvme-cli hdparm usbutils
pip install -r requirements.txt --break-system-packages   # or use a venv
sudo DRIVESTATION_MODE=real python run.py
```

Optional:

- `DRIVESTATION_SLOTS=/path/to/slots.toml` — override slot map
- `DRIVESTATION_DB=/var/lib/drivestation/drivestation.db` — job log location
- `DRIVESTATION_NO_PYUDEV=1` — poll-only detection (pyudev still recommended)

Everything is on one LAN URL (printed at startup), for example:

```
http://192.168.1.200:8330/              board (kiosk)
http://192.168.1.200:8330/logs          wipe job log + eBay blurbs
http://192.168.1.200:8330/d/<SERIAL>    per-drive listing card + PNG download
http://192.168.1.200:8330/files/        characterization reports / text dumps
http://192.168.1.200:8330/api/records.csv
```

Use **http://** (not https). Same Wi‑Fi/LAN as the mini PC.

**eBay / listing URL:** after a PASSED wipe, open
`http://<station-ip>:8330/d/<SERIAL>` (alias `/s/<SERIAL>`). Use **Download PNG**
for the listing image. Optional nicer hostname: set the mini PC’s hostname to
`drivestation` and install Avahi (`avahi-daemon`) so phones can use
`http://drivestation.local:8330/d/<SERIAL>` on the LAN — there is no public
DNS name unless you add one yourself.

### NVMe health over USB (RTL9210)

Realtek tunnels NVMe admin via SCSI opcode `0xE4` (`smartctl -d sntrealtek`).
**Identify often works; SMART Get Log Page is firmware-dependent.** Phase 0
initially probed health *without* `-d sntrealtek` (a miss). Re-check with:

```
sudo bash tools/nvme_health_probe.sh /dev/sdX
sudo apt install sg3-utils   # for raw CDB dump
```

If `Percentage Used` appears, the app will grade wear. If not, the dock
firmware is blocking Get Log Page — options are firmware update, a known-good
single-bay RTL9210B reader for grading, or accept UNKNOWN on those docks.

### Pre-wipe data + post-wipe verify

On insert the station probes used space (read-only mount / `lsblk` FSUSED when
possible, otherwise “partitions / data present”). The READY tile shows e.g.
`62GB / 256GB used` before you confirm wipe.

After every wipe it **verifies**:
- **Zero overwrite** — many multi‑MB samples must be all zeros, and no
  MBR/GPT/filesystem signatures may remain.
- **ATA secure erase** — drive still identifies, and partition/FS signatures
  must be gone (full zero-fill is not required; some SSDs return non-zero).

### Wipe methods (Phase 0 locked)

| Dock | Method |
|------|--------|
| SATA-1 / SATA-2 (ASMedia) | ATA enhanced secure erase when not frozen; else zero overwrite |
| NVME-A/B (RTL9210) | Zero overwrite only (NVMe admin blocked by bridge) |
| M2-1 SATA media (RTL9220) | ATA enhanced when available; else overwrite |
| M2-1 NVMe media | Zero overwrite only |

SATA dock is **not** hot-swap: power-cycle the port after inserting/removing.
NVMe docks and M2-1 are hot-swap.

### Phase 0 tools (already run for this hardware)

```
sudo tools/dock_characterize.sh          # per-slot bridge report
sudo tools/dock_characterize.sh --dual   # SATA LUN map (256 vs 512)
bash tools/serve_reports.sh              # pull reports over LAN :2020
```

## Layout

```
config/slots.toml          frozen USB ID_PATH allowlist (Phase 0)
drivestation/
  models.py
  station.py
  db.py
  health/policy.py
  wipe/methods.py
  hw/base.py
  hw/simulator.py
  hw/linux.py              real backend
  hw/slots_config.py
  hw/sysfs.py
  hw/identify.py
  hw/wipe_linux.py
  web/
tools/
  dock_characterize.sh
  serve_reports.sh
tests/
```
