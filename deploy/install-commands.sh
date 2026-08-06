#!/usr/bin/env bash
# Install short commands onto PATH as real copies (NOT symlinks into the
# git checkout). That way `git pull` never dirties the tree and never
# "removes" the commands when something gets stashed.
#
#   sudo bash deploy/install-commands.sh
# Then from anywhere:
#   sudo debugkiosk
#   sudo fixkiosk
#   sudo startkiosk
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run: sudo bash deploy/install-commands.sh" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Remember where the checkout lives so debugkiosk can refresh itself after pull.
echo "$REPO" > /etc/drivestation-repo-path

for cmd in debugkiosk fixkiosk startkiosk fixstation syncstation; do
    if [[ ! -f "$REPO/$cmd" ]]; then
        echo "missing $REPO/$cmd" >&2
        exit 1
    fi
    # Copy — do not chmod/symlink the git working tree (that caused stash hell).
    install -m 755 "$REPO/$cmd" "/usr/local/bin/$cmd"
    echo "installed /usr/local/bin/$cmd"
done

echo
echo "Done. Normal day-to-day:"
echo "  git -C ~/drive-station pull && sudo syncstation"
echo "  (then tell Cursor to read http://<ip>:2021/)"
