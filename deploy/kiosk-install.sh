#!/usr/bin/env bash
# Fullscreen Drive Station board on the mini PC's own monitor(s).
#
#   sudo bash deploy/kiosk-install.sh
#
# Chromium starts fullscreen (not locked kiosk mode) so Alt+F4 closes it.
# Restart=on-failure means a clean Alt+F4 exit stays closed until you
# `systemctl start drivestation-kiosk` or reboot.
#
# If a second monitor is connected, a second fullscreen window opens on
# /wipe (WIPE ONLY docks). Primary shows the grade board (/).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/kiosk-install.sh" >&2
    exit 1
fi

echo "Refreshing apt indexes (avoids stale trixie-security 404s)..."
apt-get update

echo "Installing cage + chromium + curl..."
apt-get install -y curl || true
if ! apt-get install -y cage chromium; then
    echo "chromium package failed — trying firefox-esr as the kiosk browser..."
    apt-get install -y cage firefox-esr
    BROWSER=firefox
else
    BROWSER=chromium
fi

if ! id kiosk &>/dev/null; then
    useradd -m -s /usr/sbin/nologin kiosk
fi
usermod -aG video,input,render kiosk 2>/dev/null || true

# Pick whichever browser we actually installed.
if [[ "${BROWSER:-chromium}" == "firefox" ]] || ! command -v chromium >/dev/null; then
    cat > /usr/local/bin/drivestation-kiosk <<"EOF"
#!/usr/bin/env bash
GRADE_URL="http://127.0.0.1:8330/"
WIPE_URL="http://127.0.0.1:8330/wipe"
for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$GRADE_URL" && break
    sleep 2
done
OUTPUTS=$(wlr-randr 2>/dev/null | awk '/^[A-Z]/{print $1}' | wc -l || echo 1)
if [[ "${OUTPUTS:-1}" -ge 2 ]]; then
    firefox-esr --kiosk "$WIPE_URL" &
    exec firefox-esr --kiosk "$GRADE_URL"
else
    exec firefox-esr --kiosk "$GRADE_URL"
fi
EOF
else
    cat > /usr/local/bin/drivestation-kiosk <<"EOF"
#!/usr/bin/env bash
# Wait for the API, then fullscreen Chromium. Alt+F4 closes the window.
GRADE_URL="http://127.0.0.1:8330/"
WIPE_URL="http://127.0.0.1:8330/wipe"
for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$GRADE_URL" && break
    sleep 2
done

CHROME=(chromium
    --start-fullscreen
    --incognito
    --noerrdialogs
    --disable-infobars
    --disable-session-crashed-bubble
    --disable-pinch
    --check-for-update-interval=31536000
    --ozone-platform=wayland
)

OUTPUTS=$(wlr-randr 2>/dev/null | awk '/^[A-Z]/{print $1}' | wc -l || echo 1)
if [[ "${OUTPUTS:-1}" -ge 2 ]]; then
    "${CHROME[@]}" --window-position=1920,0 "$WIPE_URL" &
    exec "${CHROME[@]}" "$GRADE_URL"
else
    exec "${CHROME[@]}" "$GRADE_URL"
fi
EOF
fi
chmod +x /usr/local/bin/drivestation-kiosk

cat > /etc/systemd/system/drivestation-kiosk.service <<"EOF"
[Unit]
Description=Drive Station kiosk screen (cage + chromium)
After=drivestation.service systemd-user-sessions.service
Wants=drivestation.service
Conflicts=getty@tty1.service

[Service]
Type=simple
User=kiosk
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
StandardInput=tty
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
# Hand the DRM seat to tty1 before cage starts (a login on tty2 steals seat0).
ExecStartPre=/usr/bin/chvt 1
ExecStartPre=/bin/sleep 1
ExecStart=/usr/bin/cage -s -- /usr/local/bin/drivestation-kiosk
KillMode=control-group
KillSignal=SIGKILL
TimeoutStopSec=5
# Clean Alt+F4 exit → stay closed. Crash → relaunch.
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical.target
EOF

usermod -aG video,input,render kiosk 2>/dev/null || usermod -aG video,input kiosk
loginctl enable-linger kiosk 2>/dev/null || true

systemctl daemon-reload
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl stop getty@tty1.service 2>/dev/null || true
chvt 1 2>/dev/null || true
systemctl enable --now drivestation-kiosk.service
# If another tty holds seat0, force-activate the kiosk wayland session.
sleep 2
for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do
    name=$(loginctl show-session "$s" -p Name --value 2>/dev/null || true)
    typ=$(loginctl show-session "$s" -p Type --value 2>/dev/null || true)
    if [[ "$name" == "kiosk" && "$typ" == "wayland" ]]; then
        loginctl activate "$s" 2>/dev/null || true
    fi
done
systemctl set-default graphical.target

bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install-commands.sh"

echo
echo "Kiosk enabled on the mini PC screen."
echo "  Alt+F4           — close board"
echo "  sudo debugkiosk  — black screen / dump logs / restore console"
echo "  sudo fixkiosk    — just turn kiosk off"
echo "  sudo startkiosk  — turn kiosk back on"
echo "  Ctrl+Alt+F2      — text console"
