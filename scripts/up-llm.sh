#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -f .env.llm ]]; then
  umask 077
  password="$(openssl rand -hex 24)"
  printf 'INITIAL_PASSWORD=%s\n' "$password" > .env.llm
  printf 'Created .env.llm with a random dashboard password; retrieve it locally from that file.\n'
fi
if grep -q '^INITIAL_PASSWORD=REPLACE_WITH_' .env.llm; then
  printf 'Set a unique INITIAL_PASSWORD in .env.llm before starting.\n' >&2
  exit 1
fi
exec docker compose --env-file .env.llm -f compose.llm.yaml up -d --build --wait --wait-timeout 180
