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

echo "Installing cage + chromium..."
apt-get install -y cage chromium

if ! id kiosk &>/dev/null; then
    useradd -m -s /usr/sbin/nologin kiosk
fi
usermod -aG video,input,render kiosk 2>/dev/null || true

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

# Primary: grade board. If a second output exists, also open wipe board
# shifted onto it (best-effort — cage/wayland positioning varies).
OUTPUTS=$(wlr-randr 2>/dev/null | awk '/^[A-Z]/{print $1}' | wc -l || echo 1)
if [[ "${OUTPUTS:-1}" -ge 2 ]]; then
    "${CHROME[@]}" --window-position=1920,0 "$WIPE_URL" &
    exec "${CHROME[@]}" "$GRADE_URL"
else
    exec "${CHROME[@]}" "$GRADE_URL"
fi
EOF
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
StandardInput=tty
StandardOutput=journal
UtmpIdentifier=tty1
# -s: allow Escape / Alt+F4 style exit from the compositor client
ExecStart=/usr/bin/cage -s -- /usr/local/bin/drivestation-kiosk
# Clean Alt+F4 exit → stay closed. Crash → relaunch.
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical.target
EOF

systemctl daemon-reload
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl enable --now drivestation-kiosk.service
systemctl set-default graphical.target

echo
echo "Kiosk enabled on the mini PC screen."
echo "  Alt+F4  — close the board (stays closed until start/reboot)"
echo "  Grade   — http://127.0.0.1:8330/"
echo "  Wipe    — http://127.0.0.1:8330/wipe  (2nd monitor if present)"
echo "  sudo systemctl start drivestation-kiosk   # reopen after Alt+F4"
echo "  Ctrl+Alt+F2                               # text console"
