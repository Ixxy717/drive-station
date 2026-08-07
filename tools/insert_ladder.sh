#!/usr/bin/env bash
# Evidence ladder for "drive in, board blank". Read-only. No service restarts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; }
info() { printf 'INFO  %s\n' "$*"; }

echo "=== L1 USB enumeration ==="
if ! command -v lsusb >/dev/null; then
  fail "lsusb missing"
else
  count=$(lsusb | grep -vc 'root hub' || true)
  lsusb | sed 's/^/  /'
  if [[ "$count" -gt 0 ]]; then pass "non-root USB devices: $count"
  else fail "only root hubs — docks are not on the bus (power-cycle hub or: sudo bash tools/usb_rescan.sh)"
  fi
fi

echo
echo "=== L2 block devices ==="
lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN,TYPE | sed 's/^/  /'
non_os=$(lsblk -dn -o NAME,TYPE,TRAN | awk '$2=="disk" && $3=="usb" {print $1}' | wc -l)
if [[ "$non_os" -gt 0 ]]; then pass "usb disks: $non_os"
else fail "no usb disks in lsblk"
fi

echo
echo "=== L3 ID_PATH vs slots.toml ==="
python3 - <<'PY'
from drivestation.hw.slots_config import load_slots_config, path_to_slot
from drivestation.hw.sysfs import list_block_disks, scan_allowlisted, default_run_cmd
slots = load_slots_config()
p2s = path_to_slot(slots)
disks = list_block_disks(default_run_cmd)
mapped = 0
unmapped = 0
zero = 0
for d in disks:
    m = p2s.get(d.id_path)
    tag = m or "—"
    usb = "usb" in (d.id_path or "")
    if d.size_bytes <= 0 and m:
        zero += 1
        print(f"  EMPTY-BAY {d.path:10} -> {tag:8} {d.id_path}")
    elif m and d.size_bytes > 0:
        mapped += 1
        print(f"  ACTIVE    {d.path:10} -> {tag:8} {d.size_bytes} {d.id_path}")
    elif usb and not m:
        unmapped += 1
        print(f"  UNMAPPED  {d.path:10} size={d.size_bytes} {d.id_path}")
found = scan_allowlisted(p2s, default_run_cmd)
print(f"allowlisted_active={sorted(found)}")
print(f"SUMMARY mapped_active={mapped} empty_mapped_bays={zero} unmapped_usb={unmapped}")
if mapped:
    print("PASS  L3/L4 allowlisted media present")
elif zero:
    print("PASS  L3 paths match; no media seated (empty bays only)")
else:
    print("FAIL  L3/L4 no allowlisted disks — check slots.toml paths")
if unmapped:
    print("FAIL  unmapped USB disks — SUITOK/other path drift; re-characterize")
PY

echo
echo "=== L5 /api/state (non-EMPTY) ==="
if curl -fsS http://127.0.0.1:8330/api/state >/tmp/ds_state.json 2>/dev/null; then
  python3 - <<'PY'
import json
d=json.load(open("/tmp/ds_state.json"))
rows=[s for s in d["slots"] if s["status"]!="EMPTY"]
for s in rows:
    dr=s.get("drive") or {}
    print(f"  {s['slot_id']:8} {s['status']:10} {dr.get('serial')}")
print(f"non_empty={len(rows)}")
print("PASS  L5 API reachable" if True else "")
PY
else
  fail "L5 API not reachable on :8330"
fi

echo
echo "=== L6 /api/debug/hw (if deployed) ==="
curl -fsS http://127.0.0.1:8330/api/debug/hw 2>/dev/null | python3 -m json.tool 2>/dev/null | head -80 \
  || info "debug/hw not available yet — deploy latest"
