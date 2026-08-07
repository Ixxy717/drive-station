#!/usr/bin/env bash
# Restart drivestation only when no wipe/verify is in flight.
set -euo pipefail
API="${DRIVESTATION_API:-http://127.0.0.1:8330}"

if curl -fsS "$API/api/state" -o /tmp/ds_safe_state.json 2>/dev/null; then
  busy=$(python3 - <<'PY'
import json
d=json.load(open("/tmp/ds_safe_state.json"))
busy=[s["slot_id"] for s in d["slots"] if s["status"] in ("WIPING","VERIFYING")]
print(",".join(busy))
PY
)
  if [[ -n "$busy" ]]; then
    echo "REFUSING restart — active wipe/verify on: $busy" >&2
    echo "Wait for WIPED/FAILED, then retry." >&2
    exit 2
  fi
else
  echo "WARN: API unreachable — proceeding with restart" >&2
fi

systemctl restart drivestation
systemctl is-active drivestation
echo "restarted OK"
