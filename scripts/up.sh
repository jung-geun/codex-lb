#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ ! -f .env ]]; then
	cp .env.example .env
fi

docker volume inspect codex-lb-data >/dev/null 2>&1 || docker volume create codex-lb-data >/dev/null
docker network inspect codex-lb-net >/dev/null 2>&1 || docker network create codex-lb-net >/dev/null
docker network inspect backend >/dev/null 2>&1 || docker network create backend >/dev/null

docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 90
"$root/scripts/verify.sh"
