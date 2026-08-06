#!/usr/bin/env bash
# Turn the station mini PC's own screen into a fullscreen kiosk board.
#
#   sudo bash deploy/kiosk-install.sh
#
# Uses cage (a minimal Wayland kiosk compositor) + Chromium pointed at the
# local board. Works on headless Debian — no desktop environment needed.
# The drivestation service itself is installed separately (deploy/install.sh).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/kiosk-install.sh" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Installing cage + chromium..."
apt-get install -y cage chromium

# Dedicated unprivileged user for the browser.
if ! id kiosk &>/dev/null; then
    useradd -m -s /usr/sbin/nologin kiosk
fi
# cage needs seat/video access
usermod -aG video,input,render kiosk 2>/dev/null || true

# Wrapper: wait for the board to answer, then start the browser. Without
# this, a reboot race shows a Chromium error page until someone touches it.
cat > /usr/local/bin/drivestation-kiosk <<"EOF"
#!/usr/bin/env bash
URL="http://127.0.0.1:8330/"
for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$URL" && break
    sleep 2
done
exec chromium \
    --kiosk --incognito --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble --disable-pinch \
    --check-for-update-interval=31536000 \
    --ozone-platform=wayland \
    "$URL"
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
ExecStart=/usr/bin/cage -d -- /usr/local/bin/drivestation-kiosk
Restart=always
RestartSec=3

[Install]
WantedBy=graphical.target
EOF

systemctl daemon-reload
systemctl disable getty@tty1.service
systemctl enable --now drivestation-kiosk.service
systemctl set-default graphical.target

echo
echo "Kiosk enabled — the attached screen now boots straight into the board."
echo "  systemctl status drivestation-kiosk"
echo "  sudo systemctl stop drivestation-kiosk   # to get a console back (tty2: Ctrl+Alt+F2)"
