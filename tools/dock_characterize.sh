#!/usr/bin/env bash
# Phase 0 dock characterization for the drive-station mini PC.
#
# Run this ON THE LINUX MINI PC with SACRIFICIAL drives (assume all data on
# them is lost, even in non-destructive mode — mistakes happen).
#
# Non-destructive by default: gathers identity/SMART/capability info only.
# Run with --destructive to additionally attempt real secure-erase commands
# (each attempt asks for confirmation and names the drive first).
#
# Usage:
#   sudo tools/dock_characterize.sh                 # info gathering
#   sudo tools/dock_characterize.sh --destructive   # + real erase attempts
#   sudo tools/dock_characterize.sh --dual          # map dual-bay SATA LUNs
#       (insert two different-size drives at once — e.g. 256GB in bay 1,
#        512GB in bay 2 — and the script records which USB/LUN is which)
#
# Output: reports/dock-characterization-<date>/  (one file per tested slot)
#         or reports/dual-sata-map-<date>/ for --dual

set -u

DESTRUCTIVE=0
DUAL=0
for arg in "$@"; do
    case "$arg" in
        --destructive) DESTRUCTIVE=1 ;;
        --dual) DUAL=1 ;;
        -h|--help)
            sed -n '1,20p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--destructive] | --dual" >&2
            exit 1
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo)." >&2
    exit 1
fi

for tool in smartctl nvme hdparm lsblk udevadm lsusb; do
    command -v "$tool" >/dev/null || {
        echo "Missing tool: $tool  (install smartmontools nvme-cli hdparm usbutils)" >&2
        exit 1
    }
done

# Always write next to this repo, regardless of the caller's cwd (sudo/cd traps).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $DUAL -eq 1 ]]; then
    OUTDIR="$ROOT/reports/dual-sata-map-$(date +%Y%m%d-%H%M%S)"
else
    OUTDIR="$ROOT/reports/dock-characterization-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$OUTDIR"
echo "Reports -> $OUTDIR"

SLOTS=(SATA-1 SATA-2 NVME-A1 NVME-A2 NVME-B1 NVME-B2 M2-1)

# Disk names only (used for removal checks / summaries).
list_disks() { lsblk -dno NAME,TYPE | awk '$2=="disk"{print $1}' | sort; }

# Snapshot identity as "name|bytes|model|serial" per disk.
# Empty docks often leave 0-byte ghost nodes that keep the same /dev name
# after a drive is inserted + power-cycled — name-only detection misses that.
disk_snapshot() {
    while read -r name; do
        [[ -z "$name" ]] && continue
        size=$(lsblk -dbno SIZE "/dev/$name" 2>/dev/null || echo 0)
        model=$(lsblk -dno MODEL "/dev/$name" 2>/dev/null | tr '|' '/' | tr -s ' ' | sed 's/^ *//;s/ *$//')
        serial=$(lsblk -dno SERIAL "/dev/$name" 2>/dev/null | tr '|' '/' | tr -s ' ' | sed 's/^ *//;s/ *$//')
        printf '%s|%s|%s|%s\n' "$name" "${size:-0}" "${model:-}" "${serial:-}"
    done < <(list_disks)
}

disk_usable() {  # disk_usable <name>  — has real capacity (not a 0B ghost)
    local size
    size=$(lsblk -dbno SIZE "/dev/$1" 2>/dev/null || echo 0)
    [[ "${size:-0}" -gt 0 ]]
}

# Compare before/after snapshots. Prints ALL candidate device names (one per line).
# Detects: brand-new names, OR same name whose size grew from 0, OR same name
# whose model/serial changed. Dual-bay docks often change BOTH bays on one
# power-cycle — caller must disambiguate when >1 usable candidate appears.
find_changed_disks() {
    local before="$1" after="$2"
    local name asize amodel aserial bsize bmodel bserial aline
    local -A seen=()

    emit() {
        local n="$1"
        [[ -n "${seen[$n]:-}" ]] && return
        seen[$n]=1
        echo "$n"
    }

    while IFS='|' read -r name asize amodel aserial; do
        [[ -z "$name" ]] && continue
        if ! grep -q "^${name}|" <<<"$before"; then
            emit "$name"
            continue
        fi
        aline=$(grep "^${name}|" <<<"$before" || true)
        IFS='|' read -r _ bsize bmodel bserial <<<"$aline"
        if [[ "${bsize:-0}" -eq 0 && "${asize:-0}" -gt 0 ]]; then
            emit "$name"
            continue
        fi
        if [[ "${asize:-0}" -gt 0 && ( "$amodel" != "$bmodel" || "$aserial" != "$bserial" ) ]]; then
            emit "$name"
        fi
    done <<<"$after"
}

# Ask operator which of several changed disks is the intended slot.
ask_which_disk() {
    local slot_name="$1"; shift
    local candidates=("$@")
    local c size_h model serial choice i=1

    printf "\n" >&2
    echo "!! Multiple drives changed at once (common on dual-bay docks)." >&2
    echo "   Which one is physically in $slot_name?" >&2
    for c in "${candidates[@]}"; do
        size_h=$(lsblk -dno SIZE "/dev/$c" 2>/dev/null || echo "?")
        model=$(lsblk -dno MODEL "/dev/$c" 2>/dev/null)
        serial=$(lsblk -dno SERIAL "/dev/$c" 2>/dev/null)
        echo "   [$i] /dev/$c  $size_h  $model  $serial" >&2
        i=$((i + 1))
    done
    read -rp "   Enter number (or s to ignore / retry): " choice
    if [[ "$choice" == "s" ]]; then
        return 1
    fi
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#candidates[@]} )); then
        echo "${candidates[$((choice - 1))]}"
        return 0
    fi
    echo "   Invalid choice." >&2
    return 1
}

section() { echo -e "\n===== $1 =====" >>"$2"; }

run_logged() {  # run_logged <reportfile> <cmd...>
    local report="$1"; shift
    echo -e "\n\$ $*" >>"$report"
    "$@" >>"$report" 2>&1
    echo "[exit: $?]" >>"$report"
}

characterize() {  # characterize <slot> <dev> <report>
    local slot="$1" dev="$2" report="$3" devpath="/dev/$2"

    section "SLOT $slot -> $devpath" "$report"

    section "USB topology (this is what slot mapping will key on)" "$report"
    run_logged "$report" udevadm info --query=property --name="$devpath"
    # The physical port path — must be stable for this slot across reboots:
    udevadm info --query=property --name="$devpath" \
        | grep -E '^(ID_PATH|ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL)=' >>"$report"

    section "USB bridge (VID/PID -> identifies the bridge chipset)" "$report"
    run_logged "$report" lsusb

    section "Block device + SCSI/LUN view" "$report"
    run_logged "$report" lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN "$devpath"
    run_logged "$report" ls -l "/sys/block/$dev"

    section "smartctl identity (does the DRIVE's real serial pass through?)" "$report"
    run_logged "$report" smartctl -i "$devpath"
    for dtype in sat "sat,12" sntasmedia sntjmicron sntrealtek; do
        section "smartctl -d $dtype" "$report"
        run_logged "$report" smartctl -d "$dtype" -i "$devpath"
    done

    section "SMART health/attributes" "$report"
    run_logged "$report" smartctl -H -A -l error "$devpath"

    if [[ "$dev" == nvme* ]]; then
        section "NVMe controller (SANICAP/FNA decide sanitize vs format)" "$report"
        run_logged "$report" nvme id-ctrl "$devpath"
        section "NVMe smart-log" "$report"
        run_logged "$report" nvme smart-log "$devpath"
        section "NVMe sanitize-log (works only if sanitize passes the bridge)" "$report"
        run_logged "$report" nvme sanitize-log "$devpath"
    else
        section "ATA security state (frozen? erase supported? time estimate?)" "$report"
        run_logged "$report" hdparm -I "$devpath"
        hdparm -I "$devpath" 2>/dev/null | sed -n '/^Security:/,/^[A-Z]/p' >>"$report"
    fi

    if [[ $DESTRUCTIVE -eq 1 ]]; then
        section "DESTRUCTIVE TESTS" "$report"
        local model serial
        model=$(lsblk -dno MODEL "$devpath"); serial=$(lsblk -dno SERIAL "$devpath")
        echo
        echo ">>> DESTRUCTIVE test on $slot: $model ($serial) at $devpath"
        read -rp ">>> Type ERASE to attempt secure erase on this drive, anything else to skip: " ans
        if [[ "$ans" == "ERASE" ]]; then
            if [[ "$dev" == nvme* ]]; then
                echo ">>> Attempting: nvme sanitize (crypto erase)"
                run_logged "$report" nvme sanitize "$devpath" --sanact=4
                sleep 5
                run_logged "$report" nvme sanitize-log "$devpath"
                echo ">>> Attempting: nvme format with crypto erase (ses=2)"
                run_logged "$report" nvme format "$devpath" --ses=2
            else
                echo ">>> Attempting: ATA security erase via hdparm"
                run_logged "$report" hdparm --user-master u \
                    --security-set-pass DrvStn "$devpath"
                run_logged "$report" hdparm --user-master u \
                    --security-erase DrvStn "$devpath"
                # If the erase failed, try to leave the drive unlocked:
                run_logged "$report" hdparm --user-master u \
                    --security-disable DrvStn "$devpath"
            fi
            echo ">>> Post-erase identity check:"
            run_logged "$report" smartctl -i "$devpath"
        else
            echo "skipped by operator" >>"$report"
        fi
    fi
}

# --dual: map physical SATA bays → USB path / LUN using two different sizes.
dual_sata_map() {
    local report="$OUTDIR/DUAL-SATA-MAP.txt"
    local bay1_gb=256 bay2_gb=512
    local name size_b size_gb path lun model serial vendor
    local -a names=()

    {
        echo "Dual-bay SATA LUN mapping"
        echo "Timestamp: $(date -Iseconds)"
        echo
        echo "Operator places two DIFFERENT-capacity drives in the Sabrent dock"
        echo "at the same time so each physical bay gets a unique USB/LUN identity."
        echo
    } >"$report"

    echo
    echo "=== Dual-bay SATA mapping (--dual) ==="
    echo "Goal: learn which USB path/LUN is physical SATA-1 vs SATA-2."
    echo
    echo "Plan (sizes are nominal — close enough is fine):"
    echo "  Physical SATA-1 (bay 1)  ←  ~${bay1_gb}GB drive"
    echo "  Physical SATA-2 (bay 2)  ←  ~${bay2_gb}GB drive"
    echo
    read -rp "Change sizes? [enter=keep 256/512, or type e.g. 500 1000]: " sizes
    if [[ -n "$sizes" ]]; then
        # shellcheck disable=SC2086
        set -- $sizes
        bay1_gb="${1:-$bay1_gb}"
        bay2_gb="${2:-$bay2_gb}"
    fi
    echo "Using: SATA-1≈${bay1_gb}GB, SATA-2≈${bay2_gb}GB" | tee -a "$report"
    echo

    echo "Current disks:"
    lsblk -dno NAME,SIZE,MODEL,SERIAL,TRAN | sed 's/^/  /'
    echo
    echo "1) Put the ~${bay1_gb}GB drive in physical bay 1 (SATA-1)."
    echo "2) Put the ~${bay2_gb}GB drive in physical bay 2 (SATA-2)."
    echo "3) Power-cycle the SATA dock port so BOTH enumerate."
    read -rp "Press enter when both drives are up: " _

    echo | tee -a "$report"
    echo "Disks after dual insert:" | tee -a "$report"
    lsblk -dno NAME,SIZE,MODEL,SERIAL,TRAN | tee -a "$report"
    echo | tee -a "$report"

    section "Per-disk USB identity (usable disks only)" "$report"

    echo
    echo "Usable drives (non-zero size):"
    printf "  %-6s %-8s %-8s %-36s %s\n" "DEV" "SIZE" "LUN" "MODEL" "SERIAL"
    while read -r name; do
        [[ -z "$name" ]] && continue
        disk_usable "$name" || continue
        # Skip the OS disk: anything with a mounted partition under /.
        if lsblk -no MOUNTPOINT "/dev/$name" 2>/dev/null | grep -qx '/'; then
            continue
        fi
        if findmnt -n -o SOURCE / 2>/dev/null | grep -q "/dev/$name"; then
            continue
        fi

        size_b=$(lsblk -dbno SIZE "/dev/$name" 2>/dev/null || echo 0)
        size_gb=$(( (size_b + 500000000) / 1000000000 ))
        model=$(lsblk -dno MODEL "/dev/$name" 2>/dev/null | tr -s ' ')
        serial=$(lsblk -dno SERIAL "/dev/$name" 2>/dev/null | tr -s ' ')
        path=$(udevadm info --query=property --name="/dev/$name" 2>/dev/null \
            | awk -F= '$1=="ID_PATH"{print $2; exit}')
        # Trailing digit of scsi-H:C:T:L is the LUN.
        lun=$(grep -oE 'scsi-[0-9:]+' <<<"$path" | head -n1 | awk -F: '{print $NF}')
        vendor=$(udevadm info --query=property --name="/dev/$name" 2>/dev/null \
            | awk -F= '$1=="ID_VENDOR"||$1=="ID_USB_VENDOR"{print $2; exit}')

        names+=("$name")
        printf "  %-6s %-8s %-8s %-36s %s\n" \
            "$name" "${size_gb}GB" "${lun:-?}" "${model:--}" "${serial:--}"

        {
            echo
            echo "--- /dev/$name ---"
            echo "SIZE_BYTES=$size_b"
            echo "SIZE_GB≈$size_gb"
            echo "MODEL=$model"
            echo "SERIAL=$serial"
            echo "ID_PATH=$path"
            echo "LUN=${lun:-unknown}"
            echo "VENDOR=$vendor"
            udevadm info --query=property --name="/dev/$name" 2>/dev/null \
                | grep -E '^(ID_PATH|ID_SERIAL|ID_MODEL|ID_VENDOR|ID_USB_|ID_BUS)=' \
                || true
        } >>"$report"
    done < <(list_disks)

    if [[ ${#names[@]} -lt 2 ]]; then
        echo
        echo "!! Need at least 2 usable drives visible. Saw ${#names[@]}."
        echo "   Power-cycle the dock with BOTH drives inserted and re-run."
        echo "Need >=2 usable drives; saw ${#names[@]}" >>"$report"
        return 1
    fi

    # Match by nominal GB (allow ±15% — marketing GB vs GiB, and 250 vs 256, etc.)
    match_bay() {
        local target="$1" best="" best_delta=999 name size_b size_gb delta
        for name in "${names[@]}"; do
            size_b=$(lsblk -dbno SIZE "/dev/$name")
            size_gb=$(( (size_b + 500000000) / 1000000000 ))
            if (( size_gb > target )); then
                delta=$(( size_gb - target ))
            else
                delta=$(( target - size_gb ))
            fi
            # within 20% of target
            if (( delta * 100 <= target * 20 && delta < best_delta )); then
                best="$name"
                best_delta=$delta
            fi
        done
        echo "$best"
    }

    local dev1 dev2 path1 path2 lun1 lun2
    dev1=$(match_bay "$bay1_gb")
    dev2=$(match_bay "$bay2_gb")

    echo
    if [[ -z "$dev1" || -z "$dev2" || "$dev1" == "$dev2" ]]; then
        echo "!! Could not auto-match both sizes. Tell me manually which /dev is which."
        read -rp "  /dev name in physical SATA-1 (~${bay1_gb}GB): " dev1
        read -rp "  /dev name in physical SATA-2 (~${bay2_gb}GB): " dev2
        dev1=${dev1#/dev/}
        dev2=${dev2#/dev/}
    else
        echo "Auto-match by size:"
        echo "  Physical SATA-1 (~${bay1_gb}GB) → /dev/$dev1 ($(lsblk -dno SIZE,MODEL,SERIAL /dev/$dev1))"
        echo "  Physical SATA-2 (~${bay2_gb}GB) → /dev/$dev2 ($(lsblk -dno SIZE,MODEL,SERIAL /dev/$dev2))"
        read -rp "Does that match what you plugged in? [enter=yes / n=fix manually]: " ok
        if [[ "$ok" == "n" || "$ok" == "N" ]]; then
            read -rp "  /dev name in physical SATA-1: " dev1
            read -rp "  /dev name in physical SATA-2: " dev2
            dev1=${dev1#/dev/}
            dev2=${dev2#/dev/}
        fi
    fi

    path1=$(udevadm info --query=property --name="/dev/$dev1" | awk -F= '$1=="ID_PATH"{print $2; exit}')
    path2=$(udevadm info --query=property --name="/dev/$dev2" | awk -F= '$1=="ID_PATH"{print $2; exit}')
    lun1=$(grep -oE 'scsi-[0-9:]+' <<<"$path1" | head -n1 | awk -F: '{print $NF}')
    lun2=$(grep -oE 'scsi-[0-9:]+' <<<"$path2" | head -n1 | awk -F: '{print $NF}')

    {
        echo
        echo "===== CONFIRMED MAPPING ====="
        echo "SATA-1_DEV=/dev/$dev1"
        echo "SATA-1_ID_PATH=$path1"
        echo "SATA-1_LUN=${lun1:-unknown}"
        echo "SATA-1_SERIAL=$(lsblk -dno SERIAL /dev/$dev1)"
        echo "SATA-1_SIZE=$(lsblk -dno SIZE /dev/$dev1)"
        echo "SATA-2_DEV=/dev/$dev2"
        echo "SATA-2_ID_PATH=$path2"
        echo "SATA-2_LUN=${lun2:-unknown}"
        echo "SATA-2_SERIAL=$(lsblk -dno SERIAL /dev/$dev2)"
        echo "SATA-2_SIZE=$(lsblk -dno SIZE /dev/$dev2)"
        if [[ "$path1" == "$path2" ]]; then
            echo "NOTE=ID_PATH identical — bay distinction is by LUN only"
        else
            echo "NOTE=ID_PATH differs between bays (separate enumeration)"
        fi
    } | tee -a "$report"

    echo
    echo "Saved → $report"
    echo "Pull it with: bash tools/serve_reports.sh"
}

if [[ $DUAL -eq 1 ]]; then
    dual_sata_map
    echo
    read -rp "Start the LAN report server now on port 2020? [enter=yes / n=no]: " serve
    if [[ "$serve" != "n" && "$serve" != "N" ]]; then
        exec bash "$(dirname "$0")/serve_reports.sh" 2020
    fi
    exit 0
fi

echo
echo "Dock characterization. For each slot you'll be prompted to insert a"
echo "sacrificial drive. Leave all other slots as they are between prompts."
echo

for slot in "${SLOTS[@]}"; do
    echo
    # Retry loop: a missed detect shouldn't force re-running the whole script.
    # Operator can keep trying this slot until it works, or skip it.
    while true; do
        # Snapshot BEFORE prompting. Detects both new /dev names AND a ghost
        # node (0B) gaining real capacity after a power-cycle — the common
        # case for non-hot-swap docks.
        before=$(disk_snapshot)
        echo "--- $slot: insert a drive now (power-cycle the port if the dock needs it),"
        echo "    Current disks:"
        lsblk -dno NAME,SIZE,MODEL,SERIAL | sed 's/^/      /'
        read -rp "    then press enter (or type s to skip this slot): " skip
        if [[ "$skip" == "s" ]]; then
            echo "Skipped $slot by operator." >"$OUTDIR/$slot.txt"
            dev=""
            break
        fi

        echo "Waiting up to 60s for a drive with real capacity..."
        dev=""
        for i in $(seq 1 60); do
            sleep 1
            if (( i % 5 == 0 )); then printf "  %ss...\r" "$i"; fi
            after=$(disk_snapshot)
            mapfile -t changed < <(find_changed_disks "$before" "$after")
            usable=()
            ghosts=()
            for c in "${changed[@]:-}"; do
                [[ -z "$c" ]] && continue
                if disk_usable "$c"; then
                    usable+=("$c")
                else
                    ghosts+=("$c")
                fi
            done
            if [[ ${#usable[@]} -eq 1 ]]; then
                printf "\n"
                dev="${usable[0]}"
                size_h=$(lsblk -dno SIZE "/dev/$dev" 2>/dev/null || echo "?")
                echo "Detected /dev/$dev ($size_h) for $slot."
                break
            fi
            if [[ ${#usable[@]} -gt 1 ]]; then
                printf "\n"
                if picked=$(ask_which_disk "$slot" "${usable[@]}"); then
                    dev="$picked"
                    size_h=$(lsblk -dno SIZE "/dev/$dev" 2>/dev/null || echo "?")
                    echo "Using /dev/$dev ($size_h) for $slot."
                    break
                fi
                # Operator said skip/retry — fall through to retry prompt.
                dev=""
                break
            fi
            if [[ ${#ghosts[@]} -gt 0 ]]; then
                printf "  saw /dev/%s (0B ghost) — waiting for capacity...\r" "${ghosts[0]}"
            fi
        done

        if [[ -n "${dev:-}" ]]; then
            break
        fi

        echo
        echo "!! No usable drive appeared for $slot."
        echo "   (A 0B ghost node doesn't count — the dock bridge is there but no media.)"
        echo "   Tips: power-cycle the port AFTER inserting, reseat the drive, confirm dock power."
        echo "   Current disks:"
        lsblk -dno NAME,SIZE,MODEL,SERIAL | sed 's/^/      /'
        read -rp "   Retry $slot? [enter=retry / s=skip]: " again
        if [[ "$again" == "s" ]]; then
            echo "No device detected for $slot (skipped after failed detect)." \
                >"$OUTDIR/$slot.txt"
            break
        fi
        echo "Retrying $slot..."
        echo
    done

    # Skipped (either up front or after failed detects).
    [[ -z "${dev:-}" ]] && continue

    echo "Gathering data for /dev/$dev (takes ~30s)..."
    characterize "$slot" "$dev" "$OUTDIR/$slot.txt"

    echo
    read -rp "--- Now REMOVE the drive from $slot and press enter (hot-unplug test): " _
    sleep 3
    if list_disks | grep -qx "$dev"; then
        echo "!! /dev/$dev still present after removal (ghost device — bridge" \
             "only scans at power-on)." | tee -a "$OUTDIR/$slot.txt"
        section "STALE-SWAP TEST (non-hot-swap bridge behavior)" "$OUTDIR/$slot.txt"
        echo
        echo "    Ghost detected. This dock likely needs a power-cycle per swap."
        read -rp "--- Insert a DIFFERENT drive into $slot WITHOUT power-cycling, press enter (or s to skip): " sk2
        if [[ "$sk2" != "s" ]]; then
            sleep 10
            echo "Devices 15s after stale swap:" >>"$OUTDIR/$slot.txt"
            run_logged "$OUTDIR/$slot.txt" lsblk -o NAME,SIZE,MODEL,SERIAL
            # Does the ghost node now answer with the NEW drive's identity?
            run_logged "$OUTDIR/$slot.txt" smartctl -i "/dev/$dev"
            read -rp "--- Now power-cycle the port for $slot, press enter: " _
            sleep 8
            echo "Devices after power-cycle:" >>"$OUTDIR/$slot.txt"
            run_logged "$OUTDIR/$slot.txt" lsblk -o NAME,SIZE,MODEL,SERIAL
        fi
    else
        echo "Removal detected cleanly (true hot-swap)." >>"$OUTDIR/$slot.txt"
    fi
done

# Quick summary of what landed.
{
    echo "Drive Station Phase 0 — $(date -Iseconds)"
    echo "Folder: $OUTDIR"
    echo
    for f in "$OUTDIR"/*.txt; do
        [[ -f "$f" ]] || continue
        base=$(basename "$f" .txt)
        if grep -q "^===== SLOT " "$f" 2>/dev/null; then
            echo "OK     $base"
        else
            echo "SKIP   $base — $(head -n1 "$f")"
        fi
    done
} | tee "$OUTDIR/SUMMARY.txt"

echo
echo "Done. Reports are in: $OUTDIR"
echo
echo "To pull them from another PC on the same network, run:"
echo "  bash tools/serve_reports.sh"
echo "then open http://<this-machine-ip>:2020/ in your browser."
echo
read -rp "Start the LAN report server now on port 2020? [enter=yes / n=no]: " serve
if [[ "$serve" != "n" && "$serve" != "N" ]]; then
    # Drop out of the characterization script into the server.
    exec bash "$(dirname "$0")/serve_reports.sh" 2020
fi
