#!/usr/bin/env bash
# Serve the reports/ folder over the LAN so you can download Phase 0
# characterization results from another PC without USB juggling.
#
# Usage (on the mini PC):
#   bash tools/serve_reports.sh           # port 2020
#   bash tools/serve_reports.sh 8080      # custom port
#
# Then on your Windows/office PC open (HTTP, not HTTPS):
#   http://<mini-pc-ip>:2020/
#
# Ctrl+C stops the server.

set -u

PORT="${1:-2020}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/reports"
mkdir -p "$DIR"

# Build a fresh landing page + per-folder pages + zip bundles.
python3 - "$DIR" <<'PY'
import os, sys, zipfile, html
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)

def esc(s): return html.escape(str(s))

folders = sorted(
    [p for p in root.iterdir() if p.is_dir()],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)

# Per-folder index + zip
for folder in folders:
    files = sorted([f for f in folder.iterdir() if f.is_file() and f.name != "index.html"])
    zip_path = root / f"{folder.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f"{folder.name}/{f.name}")

    rows = []
    if not files:
        rows.append(
            "<p style='color:#a33'><b>This folder is empty.</b> "
            "Characterization likely failed or was interrupted before any "
            "slot finished. Re-run <code>sudo bash tools/dock_characterize.sh</code> "
            "after <code>git pull</code>.</p>"
        )
    else:
        rows.append("<ul>")
        for f in files:
            kb = max(1, f.stat().st_size // 1024)
            rows.append(
                f'<li><a href="{esc(f.name)}">{esc(f.name)}</a> '
                f'<span style="color:#666">({kb} KB)</span> '
                f'— <a href="{esc(f.name)}" download>download</a></li>'
            )
        rows.append("</ul>")
        rows.append(
            f'<p><a href="../{esc(folder.name)}.zip"><b>Download all as ZIP</b></a></p>'
        )

    (folder / "index.html").write_text(
        f"""<!DOCTYPE html>
<html><head><meta charset=utf-8><title>{esc(folder.name)}</title>
<style>
body{{font:16px/1.45 system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 16px}}
a{{color:#14589f}} li{{margin:8px 0}} code{{background:#eee;padding:1px 5px}}
</style></head><body>
<p><a href="/">&larr; all reports</a></p>
<h1>{esc(folder.name)}</h1>
{''.join(rows)}
</body></html>
""",
        encoding="utf-8",
    )

# Root index
items = []
for folder in folders:
    n = len([f for f in folder.iterdir() if f.is_file() and f.name != "index.html"])
    label = f"{folder.name}/ ({n} files)" if n else f"{folder.name}/ (EMPTY)"
    items.append(
        f'<li><a href="{esc(folder.name)}/">{esc(label)}</a>'
        + (f' — <a href="{esc(folder.name)}.zip">zip</a>' if n else "")
        + "</li>"
    )

zips = sorted(root.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
zip_links = "".join(
    f'<li><a href="{esc(z.name)}">{esc(z.name)}</a></li>' for z in zips
)

(root / "index.html").write_text(
    f"""<!DOCTYPE html>
<html><head><meta charset=utf-8><title>Drive Station Reports</title>
<style>
body{{font:16px/1.45 system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 16px}}
a{{color:#14589f}} li{{margin:8px 0}} code{{background:#eee;padding:1px 5px}}
.note{{background:#f4f4f0;border:1px solid #ccc;padding:10px 12px;margin:16px 0}}
</style></head><body>
<h1>Drive Station Reports</h1>
<div class="note">
  Use <b>http://</b> (not https). Click a folder, then a <code>.txt</code> file,
  or grab the <b>zip</b>. Empty folders mean that run never wrote slot reports —
  re-run characterization after updating.
</div>
<h2>Report folders</h2>
<ul>
{''.join(items) if items else '<li>No reports yet.</li>'}
</ul>
<h2>Zip downloads</h2>
<ul>
{zip_links if zip_links else '<li>None yet.</li>'}
</ul>
<p>Serving from <code>{esc(root)}</code></p>
</body></html>
""",
    encoding="utf-8",
)

print(f"Prepared {len(folders)} report folder(s) under {root}")
for folder in folders:
    n = len([f for f in folder.iterdir() if f.is_file() and f.name != "index.html"])
    print(f"  {folder.name}: {n} file(s)")
PY

echo
echo "Serving $DIR on port $PORT"
echo
echo "Open one of these on your other PC (HTTP — not HTTPS):"
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
# --directory keeps links stable; bind all interfaces for LAN access.
exec python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$DIR"
