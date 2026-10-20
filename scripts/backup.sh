#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container="${CODEX_LB_CONTAINER:-$(docker compose -f "$root/compose.yaml" --project-directory "$root" ps -q codex-lb)}"
if [[ -z "$container" ]]; then
	echo "codex-lb compose container is not running" >&2
	exit 1
fi
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$root/backups/codex-lb-$timestamp"
partial="$destination.partial"
container_copy="/tmp/store-$timestamp.db"

cleanup() {
	docker exec -u 0 "$container" rm -f "$container_copy" >/dev/null 2>&1 || true
	rm -rf "$partial"
}
trap cleanup EXIT

mkdir -p "$root/backups"
mkdir "$partial"
chmod 0700 "$partial"
docker inspect "$container" >/dev/null
docker exec "$container" /opt/venv/bin/python -c '
import sqlite3
import sys

source = sqlite3.connect("file:/var/lib/codex-lb/store.db?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[1])
with destination:
    source.backup(destination)
integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
if integrity != "ok":
    raise SystemExit(f"backup integrity check failed: {integrity}")
print("database_integrity=ok")
' "$container_copy"

docker cp "$container:$container_copy" "$partial/store.db"
docker cp "$container:/var/lib/codex-lb/encryption.key" "$partial/encryption.key"
docker exec -u 0 "$container" rm -f "$container_copy"
chmod 0600 "$partial/store.db" "$partial/encryption.key"

python3 - "$partial/store.db" "$partial/encryption.key" <<'PY'
import pathlib
import sqlite3
import sys

database = pathlib.Path(sys.argv[1])
key = pathlib.Path(sys.argv[2])
integrity = sqlite3.connect(f"file:{database}?immutable=1", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
if integrity != "ok":
    raise SystemExit(f"copied database integrity check failed: {integrity}")
if not key.read_bytes():
    raise SystemExit("copied encryption key is empty")
print("copied_artifacts=ok")
PY
rm -f "$partial/store.db-wal" "$partial/store.db-shm"

(
	cd "$partial"
	shasum -a 256 store.db encryption.key > SHA256SUMS
)
chmod 0600 "$partial/SHA256SUMS"
mv "$partial" "$destination"
trap - EXIT

echo "backup=$destination"
echo "contents=store.db,encryption.key,SHA256SUMS"
