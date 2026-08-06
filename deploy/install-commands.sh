#!/usr/bin/env bash
# Drop short commands on PATH so you never need cd:
#   sudo debugkiosk
#   sudo fixkiosk
#   sudo startkiosk
#
#   sudo bash deploy/install-commands.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run: sudo bash deploy/install-commands.sh" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for cmd in debugkiosk fixkiosk startkiosk; do
    if [[ ! -f "$REPO/$cmd" ]]; then
        echo "missing $REPO/$cmd" >&2
        exit 1
    fi
    chmod +x "$REPO/$cmd"
    ln -sfn "$REPO/$cmd" "/usr/local/bin/$cmd"
    echo "installed /usr/local/bin/$cmd → $REPO/$cmd"
done

echo
echo "Done. From anywhere:"
echo "  sudo debugkiosk   # black screen / dump logs / restore console"
echo "  sudo fixkiosk     # just turn kiosk off"
echo "  sudo startkiosk   # turn kiosk back on"
