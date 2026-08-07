# drive-station

Drive testing and secure-wiping station for an electronics recycling / ITAD bench.

One mini PC + USB docks (StarTech 4-bay SATA + 2× StarTech NVMe toasters +
2× SUITOK dual NVMe + 1× M.2) = an 11-slot appliance. Operators insert drives,
look at the screen, click YES/NO, and remove finished drives.

## Roadmap

- **Phase 0 — done.** Hardware characterization, slot allowlist, safe wipe +
  verify engine, health grading, LAN board, job log, listing cards.
- **Phase 1 — done (this release).** Appliance behavior: systemd service,
  kiosk screen on the station's own monitor, printable NIST 800-88
  certificates of destruction with QR codes, batch/client-lot tagging,
  pre-wired support for the incoming ASM2362 NVMe and SAS docks.
- **Phase 2 — blocked on new hardware.** Characterize the ASM2362 and SAS
  docks, add their slots, NVMe wear grading on the new dock, SAS grading in
  production; explore NVMe sanitize / SCSI FORMAT UNIT where the bridges
  allow it; read-speed test as an extra grading signal.
- **Phase 3 — business layer.** Inventory states (received → wiped → listed →
  sold), pricing suggestions, label printing (QR stickers), multi-station
  roll-up.

## How it works

- **Slot mapping**: every physical dock slot has a permanent name (`SATA-1`,
  `NVME-A1`, `M2-1`, ...). On Linux, slots are identified by the USB `ID_PATH`
  (including SCSI LUN when a dock exposes multiple bays), never by `/dev/sdX` names.
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

### Run as a service (recommended)

```
sudo bash deploy/install.sh
```

Installs a systemd unit so the board starts at boot and restarts on crash —
no more dying with the SSH session. Manage it with:

```
systemctl status drivestation
journalctl -u drivestation -f     # live logs
sudo systemctl restart drivestation   # after a git pull
```

### Kiosk screen on the station itself

```
sudo bash deploy/kiosk-install.sh
```

The mini PC's attached monitor boots into a **fullscreen** board (cage +
Chromium). **Alt+F4** closes it. A second monitor, if present, opens `/wipe`.

**Stuck on a black screen?** (no `cd` — commands are copies on PATH)

```
sudo debugkiosk     # stop kiosk, restore console, start LAN dump server :2021
sudo fixkiosk       # just turn kiosk off
sudo startkiosk     # turn it back on
```

After `debugkiosk`, the dump is at `http://<mini-pc-ip>:2021/kiosk-debug.txt`.

One-time / after pull: `sudo bash deploy/install-commands.sh`  
(Copies into `/usr/local/bin` — does **not** dirty the git tree.)

| URL | What |
|-----|------|
| `/` | Grade board — StarTech NVMe A/B, SATA, M2 (no wipe-only docks) |
| `/wipe` | Wipe-only SUITOK docks + queued serials (2nd monitor) |

Workflow: grade on StarTech → **QUEUE WIPE** / auto-queue if ≥1TB → move stick
to a WIPE ONLY bay → it shows `FROM NVME-A1` and asks to wipe.

Optional:

- `DRIVESTATION_SLOTS=/path/to/slots.toml` — override slot map
- `DRIVESTATION_ORG="Your Company LLC"` — name printed on certificates
- `DRIVESTATION_DB=/var/lib/drivestation/drivestation.db` — job log location
- `DRIVESTATION_NO_PYUDEV=1` — poll-only detection (pyudev still recommended)

Everything is on one LAN URL (printed at startup), for example:

```
http://192.168.1.200:8330/                 board (kiosk)
http://192.168.1.200:8330/logs             wipe job log + eBay blurbs + batch control
http://192.168.1.200:8330/d/<SERIAL>       per-drive listing card + PNG download
http://192.168.1.200:8330/cert/<SERIAL>    printable Certificate of Data Destruction
http://192.168.1.200:8330/files/           characterization reports / text dumps
http://192.168.1.200:8330/api/records.csv
```

**Certificates:** `/cert/<SERIAL>` is a print-ready Certificate of Data
Destruction using NIST SP 800-88 Rev. 1 terminology (Clear for verified
overwrite, Purge for ATA secure erase / NVMe sanitize), with a QR code back
to the live record. Print or save as PDF from the browser. Set
`DRIVESTATION_ORG` for the company name on the letterhead.

**Batches / client lots:** set an active batch on the logs page (or
`POST /api/batch`); every wipe started while it's active is stamped with it.
Filter the log and CSV per batch for per-client reporting.

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

### Wipe methods

| Dock | Method |
|------|--------|
| SATA-1…4 (StarTech 4-bay) | ATA enhanced when not frozen; else zero overwrite; **hot-swap** |
| NVME-A1 / NVME-B1 (StarTech) | Zero overwrite; SMART wear via `-d sntasmedia` |
| SUITOK-1…4 | Zero overwrite; wear often UNKNOWN |
| M2-1 SATA media (RTL9220) | ATA enhanced when available; else overwrite |
| M2-1 NVMe media | Zero overwrite only |

### Slot names (match the bench)

| Slots | Hardware | Bridge |
|-------|----------|--------|
| `NVME-A1`, `NVME-B1` | StarTech NVMe toasters (grade — real wear %) | `asm2362` |
| `SATA-1`…`4` | StarTech SDOCK4U313 4-bay SATA (replaced Sabrent) | `asmedia_sata` |
| `M2-1` | M.2 dock | `rtl9220` |
| `SUITOK-1`…`4` | SUITOK wipe-only | `rtl9210` (or `rtl9220`) |

```
git pull
sudo systemctl restart drivestation

# Guided remap (one bay at a time)
sudo tools/dock_characterize.sh

# StarTech 4-bay → SATA-1..4 (four different-size drives at once)
sudo tools/dock_characterize.sh --quad
```

Paste the printed `[slots.*]` / `ID_PATH=` lines into `config/slots.toml`, then
`sudo systemctl restart drivestation`. Confirm StarTech NVMe wear:

```
sudo smartctl -a -d sntasmedia /dev/sdX
```

If `sntasmedia` fails but `sntrealtek` works, set that slot to
`bridge = "rtl9210"` instead of `asm2362`.

StarTech NVMe, StarTech 4-bay SATA, SUITOK, and M2-1 are hot-swap.

### Phase 0 tools (already run for this hardware)

```
sudo tools/dock_characterize.sh          # per-slot bridge report
sudo tools/dock_characterize.sh --quad   # StarTech 4-bay → SATA-1..4
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
