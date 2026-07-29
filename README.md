# drive-station

Drive testing and secure-wiping station for an electronics recycling / ITAD bench.

One mini PC + USB docks (2× SATA bays, 4× NVMe M.2 slots, 1× M.2 NVMe/SATA slot)
= a 7-slot appliance. Operators insert drives, look at the screen, click YES/NO,
and remove finished drives. Everything else is automatic.

## How it works

- **Slot mapping**: every physical dock slot has a permanent name (`SATA-1`,
  `NVME-A1`, `M2-1`, ...). On Linux, slots are identified by the USB *port path*
  (physical port chain), never by `/dev/sdX` names. Devices not on the dock
  allowlist are completely ignored.
- **Simulator mode**: on Windows (or anywhere without the real docks) the app
  runs against fake docks and fake drives, including injectable faults
  (drive yanked mid-wipe, serial swap, verify failure, ...). The entire UI and
  state machine are testable without touching hardware.
- **Safety**: wipes are bound to slot + serial. The serial is re-read and
  re-verified immediately before any destructive command. Any ambiguity stops
  the job. The OS drive and non-allowlisted USB devices are structurally
  unreachable.

## Running (development, simulator mode)

```
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8330 — the kiosk board plus a simulator control panel
for inserting fake drives and injecting faults.

## Tests

```
python -m pytest
```

The test suite deliberately tries to trick the software into wiping the wrong
(fake) drive. All edge cases live in `tests/`.

## Deploying to the station mini PC (Linux)

```
git clone <this repo> && cd drive-station
pip install -r requirements.txt
DRIVESTATION_MODE=real python run.py
```

Updates are `git pull` on the mini PC.

**Before real mode works**, run the Phase 0 dock characterization on the mini
PC with sacrificial drives:

```
sudo tools/dock_characterize.sh
```

Its report determines which wipe commands actually pass through each dock's
USB bridge and how the slots enumerate. The Linux hardware backend is built
from those results.

## Layout

```
drivestation/
  models.py        drive/slot/status data types
  station.py       slot state machine + safety-gated wipe engine
  db.py            SQLite job log (traceable by serial, batch-ready)
  health/policy.py health scoring (thresholds are placeholders, NOT final)
  wipe/methods.py  wipe method preference per drive type
  hw/base.py       hardware backend interface
  hw/simulator.py  fake docks/drives with fault injection
  hw/linux.py      real backend (pending Phase 0 dock characterization)
  web/             FastAPI app + kiosk UI
tools/
  dock_characterize.sh   Phase 0 dock/bridge test harness (run on mini PC)
tests/                   edge-case test suite
```
