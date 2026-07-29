#!/usr/bin/env bash
# Serve the reports/ folder over the LAN so you can download Phase 0
# characterization results from another PC without USB juggling.
#
# Usage (on the mini PC):
#   bash tools/serve_reports.sh           # port 2020
#   bash tools/serve_reports.sh 8080      # custom port
#
# Then on your Windows/office PC open:
#   http://<mini-pc-ip>:2020/
#
# Ctrl+C stops the server.

set -u

PORT="${1:-2020}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/reports"

mkdir -p "$DIR"

# Write a tiny index so the landing page isn't a raw directory dump.
INDEX="$DIR/index.html"
{
    echo "<!DOCTYPE html><html><head><meta charset=utf-8>"
    echo "<title>Drive Station Reports</title>"
    echo "<style>body{font:16px/1.4 system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px}"
    echo "a{color:#14589f} li{margin:6px 0} code{background:#eee;padding:1px 5px}</style></head><body>"
    echo "<h1>Drive Station Reports</h1>"
    echo "<p>Click a folder, then open the <code>.txt</code> files (or download them).</p><ul>"
    # Newest first.
    find "$DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' 2>/dev/null \
        | sort -rn | while read -r _ name; do
        echo "<li><a href=\"${name}/\">${name}/</a></li>"
    done
    # Also list loose files in reports/ root.
    find "$DIR" -mindepth 1 -maxdepth 1 -type f ! -name index.html -printf '%f\n' 2>/dev/null \
        | sort | while read -r name; do
        echo "<li><a href=\"${name}\">${name}</a></li>"
    done
    echo "</ul><p>Serving from <code>$DIR</code></p></body></html>"
} >"$INDEX"

echo "Serving $DIR on port $PORT"
echo
echo "Open one of these on your other PC:"
# Prefer global/LAN addresses; skip localhost-only.
ips=$(hostname -I 2>/dev/null || true)
if [[ -z "$ips" ]]; then
    ips=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
fi
if [[ -z "$ips" ]]; then
    echo "  (could not detect LAN IP — run: hostname -I)"
else
    for ip in $ips; do
        echo "  http://${ip}:${PORT}/"
    done
fi
echo
echo "Ctrl+C to stop."
echo

cd "$DIR"
exec python3 -m http.server "$PORT" --bind 0.0.0.0
