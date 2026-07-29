#!/usr/bin/env bash
# Exhaustive NVMe-over-USB health probe for RTL9210 docks.
# Run ON the mini PC with a drive in any NVME-* slot:
#   sudo bash tools/nvme_health_probe.sh /dev/sdX
#   sudo bash tools/nvme_health_probe.sh          # auto-pick first Realtek NVMe
#
# Writes a report next to the repo and prints a short verdict.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="$ROOT/reports/nvme-health-probe-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTDIR"
REPORT="$OUTDIR/PROBE.txt"

pick_dev() {
    if [[ -n "${1:-}" ]]; then
        echo "$1"
        return
    fi
    # Prefer udev ID_USB_MODEL containing RTL9210 / Realtek NVMe
    for d in /dev/sd?; do
        [[ -b "$d" ]] || continue
        info="$(udevadm info --query=property --name="$d" 2>/dev/null || true)"
        if echo "$info" | grep -qiE 'RTL9210|0bda:9210|Realtek.*NVME'; then
            # skip zero-size ghosts
            sz="$(lsblk -dbno SIZE "$d" 2>/dev/null | head -1 || echo 0)"
            [[ "${sz:-0}" -gt 0 ]] || continue
            echo "$d"
            return
        fi
    done
    echo ""
}

DEV="$(pick_dev "${1:-}")"
if [[ -z "$DEV" || ! -b "$DEV" ]]; then
    echo "No Realtek NVMe /dev/sdX found. Pass device: $0 /dev/sdX" >&2
    exit 1
fi

{
    echo "===== NVMe USB health probe ====="
    echo "device=$DEV"
    echo "date=$(date -Is)"
    echo "smartctl=$(smartctl -V | head -1)"
    echo
    echo "===== udev / usb ====="
    udevadm info --query=property --name="$DEV" | grep -E 'ID_PATH|ID_VENDOR|ID_MODEL|ID_USB|ID_SERIAL|ID_BUS' || true
    echo
    lsusb | grep -i realtek || true
    echo
    echo "===== kernel driver (uas vs usb-storage) ====="
    # /sys/block/sdX -> ... -> driver
    SYS="$(readlink -f "/sys/block/$(basename "$DEV")" || true)"
    echo "sys=$SYS"
    find "$SYS/device" -maxdepth 5 -name driver -type l 2>/dev/null | while read -r link; do
        echo "$link -> $(readlink -f "$link")"
    done || true
    lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN,ROTA "$DEV" || true
    echo

    try() {
        local title="$1"; shift
        echo "===== $title ====="
        echo "\$ $*"
        set +e
        "$@"
        local rc=$?
        set -e
        echo "[exit: $rc]"
        echo
        return 0
    }

    try "smartctl -d test" smartctl -d test "$DEV"
    try "smartctl -i (auto)" smartctl -i "$DEV"
    try "smartctl -i -d sntrealtek" smartctl -i -d sntrealtek "$DEV"
    try "smartctl -H -A (NO -d) — Phase0 mistake" smartctl -H -A -l error "$DEV"
    try "smartctl -a -d sntrealtek  ★ main hope" smartctl -a -d sntrealtek "$DEV"
    try "smartctl -x -d sntrealtek" smartctl -x -d sntrealtek "$DEV"
    try "smartctl -H -A -d sntrealtek" smartctl -H -A -d sntrealtek "$DEV"
    try "smartctl -a -d sntrealtek -j" smartctl -a -d sntrealtek -j "$DEV"
    try "smartctl -a -d auto" smartctl -a -d auto "$DEV"
    try "smartctl -l ssd -d sntrealtek" smartctl -l ssd -d sntrealtek "$DEV"
    try "smartctl -l farm -d sntrealtek" smartctl -l farm -d sntrealtek "$DEV"

    # Raw Realtek NVMe tunnel (SCSI CDB 0xE4) — Get Log Page SMART (LID 0x02)
    # Matches smartmontools sntrealtek_device::nvme_pass_through
    if command -v sg_raw >/dev/null 2>&1; then
        try "sg_raw Identify Controller (opcode 06 CDW10=01)" \
            sg_raw -r 4k "$DEV" E4 00 10 06 01 00 00 00 00 00 00 00 00 00 00 00
        try "sg_raw Get Log Page SMART (opcode 02 LID 02)" \
            sg_raw -r 512 "$DEV" E4 00 02 02 02 00 00 00 00 00 00 00 00 00 00 00
        # Also try 0x200 length encoding like smartctl (size in cdb[1..2] LE)
        try "sg_raw Get Log size=0x200" \
            sg_raw -r 512 "$DEV" E4 00 02 02 02 00 00 00 00 00 00 00 00 00 00 00
    else
        echo "===== sg_raw not installed (apt install sg3-utils) ====="
        echo
    fi

    echo "===== grep interesting lines ====="
    grep -nEi 'Percentage Used|Available Spare|Critical Warning|Media and Data|unsupported|SMART/Health|Temperature|percentage_used' "$REPORT" || true
} | tee "$REPORT"

echo
echo "Report -> $REPORT"
if grep -qiE 'Percentage Used:\s*[0-9]+' "$REPORT"; then
    echo "VERDICT: HEALTH DATA FOUND (Percentage Used present). Wire this into the app."
    grep -iE 'Percentage Used|Available Spare|Critical Warning|Media and Data' "$REPORT" | head -20
elif grep -qi 'nvme_smart_health_information_log' "$REPORT" && grep -qi 'percentage_used' "$REPORT"; then
    echo "VERDICT: JSON health log present."
else
    echo "VERDICT: No Percentage Used yet — firmware may block Get Log Page (Identify often still works)."
    echo "Next levers: ensure Driver=uas (not usb-storage), try Realtek dock firmware update, or a single-bay RTL9210B reader for grading."
fi
