#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

cid="$(docker compose ps -q codex-lb)"
if [[ -z "$cid" ]]; then
	echo "codex-lb compose container is not running" >&2
	exit 1
fi

running="$(docker inspect --format '{{.State.Running}}' "$cid")"
health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$cid")"
if [[ "$running" != true || "$health" != healthy ]]; then
	echo "codex-lb is not ready: running=$running health=$health" >&2
	exit 1
fi

http_code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:2455/health/ready)"
if [[ "$http_code" != 200 ]]; then
	echo "codex-lb readiness returned HTTP $http_code" >&2
	exit 1
fi

docker compose exec -T codex-lb /opt/venv/bin/python -c '
from pathlib import Path
from app.main import create_app
assert "69f128afcbc616d9f8e924ca6583f7031d75cf82" in Path("/app/UPSTREAM_REVISION").read_text()
assert Path("/app/app/static/index.html").is_file()
routes = {route.path for route in create_app().routes}
assert {"/v1/messages", "/v1/messages/count_tokens", "/v1/omp/models"} <= routes
print("source_build=ok dashboard_assets=ok gateway_routes=ok")
'

manifest_code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:2455/v1/omp/models)"
if [[ "$manifest_code" != 401 ]]; then
	echo "unauthenticated OMP manifest returned HTTP $manifest_code, expected 401" >&2
	exit 1
fi

echo "verify ok: container=$cid running=true health=healthy readiness=200 source_build=ok manifest_auth=401"
