#!/usr/bin/env bash
# Install drive-station as a systemd service on the station mini PC.
#
#   cd ~/drive-station
#   sudo bash deploy/install.sh
#
# After this the board survives reboots and SSH disconnects:
#   systemctl status drivestation
#   journalctl -u drivestation -f
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/install.sh" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"

if [[ ! -f "$REPO/run.py" ]]; then
    echo "Cannot find run.py under $REPO — run from the repo checkout." >&2
    exit 1
fi
if [[ ! -f "$REPO/config/slots.toml" ]]; then
    echo "Missing config/slots.toml — real mode will not start without it." >&2
    exit 1
fi

# Board deps (system python used by the service).
python3 -m pip install -r "$REPO/requirements.txt" --break-system-packages \
    || pip3 install -r "$REPO/requirements.txt" --break-system-packages \
    || true

sed -e "s|@REPO@|$REPO|g" -e "s|@PY@|$PY|g" \
    "$REPO/deploy/drivestation.service" \
    > /etc/systemd/system/drivestation.service

systemctl daemon-reload
systemctl enable --now drivestation

# Short commands on PATH: sudo debugkiosk / fixkiosk / startkiosk
bash "$REPO/deploy/install-commands.sh"

echo
echo "Installed. Board: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8330/"
echo "Kiosk stuck/black?  sudo debugkiosk"
systemctl --no-pager --lines=5 status drivestation || true
