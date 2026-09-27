#!/usr/bin/env bash
# Read-only diagnosis of the Telegram alerts that still arrive (run on the CONTROLLER as
# hermes, not root; asks for the sudo password once). Changes nothing. Prints no secret
# values: env files are shown as key names with non-secret values only, and outbox rows
# as topic/status/time counts without payloads.
#
#   cd ~/engineering/engineering-control && .go-status/telegram-alerts-diagnose.sh
set -euo pipefail
trap 'echo "FAILED at line $LINENO (exit $?)" >&2' ERR

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as hermes, not root (it calls sudo itself)." >&2
  exit 1
fi

echo "=== 1/5 watchdog settings (engineering-watchdog reads control.env + watchdog-recovery.env) ==="
sudo bash -c '
  for f in /etc/engineering-control/env/control.env /etc/engineering-control/env/watchdog-recovery.env; do
    echo "-- $f"
    grep -E "^ENGINEERING_(WATCHDOG_|PROJECTION_|NOTIFICATION_)" "$f" 2>/dev/null \
      | sed -E "s#(URL=[a-z]+://[^/]*)/.*#\1/<path redacted>#; s#(TOKEN[A-Z_]*=).*#\1<redacted>#" || echo "   (no watchdog/projection/notification keys)"
  done'

echo "=== 2/5 watchdog state: one line per probe ==="
sudo python3 - <<'PY'
import json
state = json.load(open("/var/lib/engineering-watchdog/state.json"))
print("updated_at", state.get("updated_at"))
for key, p in sorted(state.get("probes", {}).items()):
    if not isinstance(p, dict):
        continue
    print(f"{key:34} active={str(p.get('active')):5} status={str(p.get('status')):20} "
          f"last_alerted={str(p.get('last_alerted_at'))[:19]:19} pending={p.get('pending_event')} "
          f"last_observed={str(p.get('last_observed_at'))[:19]}")
PY

echo "=== 3/5 watchdog journal, last 72 h (delivery results only) ==="
sudo journalctl -u engineering-watchdog --since "-72h" --no-pager -o short-iso 2>/dev/null \
  | grep -iE "deliver|alert|recovery|change|degraded|unhealthy|error" | tail -40 || echo "(no matching lines)"

echo "=== 4/5 notification outbox, last 72 h: topic x status (no payloads) ==="
sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
  -p EnvironmentFile=/etc/engineering-control/env/control.env \
  /opt/engineering-control/venv/bin/python - <<'PY'
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.config import get_settings
from engineering_control.db import models as db

since = datetime.now(UTC) - timedelta(hours=72)
handler = EngineeringActivityHandler(get_settings())
with handler.sessions() as session:
    rows = session.scalars(
        select(db.NotificationOutbox).where(db.NotificationOutbox.created_at >= since)
    ).all()
    counts = Counter((row.topic, row.status) for row in rows)
    for (topic, status), n in sorted(counts.items()):
        print(f"{n:5}  {topic:50} {status}")
    by_hour = Counter(row.created_at.strftime("%m-%d %H:00") for row in rows)
    print("per hour:", dict(sorted(by_hour.items())[-24:]))
PY

echo "=== 5/5 backup status used by the watchdog's backup-recovery probe ==="
sudo python3 - <<'PY'
import json
v = json.load(open("/var/lib/engineering-backup/status.json"))
print({k: v.get(k) for k in ("status", "identifier", "last_backup_completed_at_utc",
                             "last_transfer_verified_at_utc", "last_restore_verified_at_utc",
                             "worker_mirror_verified", "worker_receipt_signature_verified")})
PY
echo "Done. Paste the whole output back."
