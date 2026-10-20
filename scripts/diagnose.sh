#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

verify_status=0
"$root/scripts/verify.sh" || verify_status=$?
container_id="$(docker compose ps -q codex-lb)"
started_at="$(docker inspect --format '{{.State.StartedAt}}' "$container_id")"


echo
docker compose exec -T codex-lb /opt/venv/bin/python -c '
import datetime
import sqlite3
import sys

started_at = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
cutoff = started_at.astimezone(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
connection = sqlite3.connect("file:/var/lib/codex-lb/store.db?mode=ro", uri=True)

def show(title, query, parameters):
    print(title)
    rows = connection.execute(query, parameters).fetchall()
    if not rows:
        print("  none")
    for row in rows:
        print("  " + repr(row))

columns = """
    SELECT id, requested_at, model, transport, status,
           COALESCE(error_code, ""), SUBSTR(COALESCE(error_message, ""), 1, 220)
      FROM request_logs
"""
status_signature = """
       INSTR(COALESCE(error_message, ?), ?) > 0
   AND INSTR(COALESCE(error_message, ?), ?) > 0
"""
signature_parameters = ("", "input[", "", ".status")

print(f"Current container started at UTC: {cutoff}")
show(
    "Post-start compaction status replay errors (current deployment):",
    columns + " WHERE requested_at >= ? AND " + status_signature + " ORDER BY id DESC LIMIT 20",
    (cutoff, *signature_parameters),
)
print()
show(
    "Historical compaction status replay errors (before current deployment):",
    columns + " WHERE requested_at < ? AND " + status_signature + " ORDER BY id DESC LIMIT 20",
    (cutoff, *signature_parameters),
)
print()
show(
    "Post-start client disconnect cancellations (downstream aborts; not proof of this defect):",
    columns + " WHERE requested_at >= ? AND error_code = ? ORDER BY id DESC LIMIT 20",
    (cutoff, "client_disconnected"),
)
' "$started_at"

exit "$verify_status"
