#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 0 ]]; then
	echo "usage: $0" >&2
	exit 2
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
upstream="$root/gateway"
python="$upstream/.venv/bin/python"
if [[ ! -x "$python" ]]; then
	echo "run uv sync --frozen in gateway/ first" >&2
	exit 1
fi

PYTHONPATH="$upstream" "$python" -m pytest \
	-c "$upstream/pyproject.toml" -p tests.conftest \
	"$root/tests/test_websocket_lifetime.py" \
	"$root/tests/test_auth_quarantine.py" \
	"$root/tests/test_operation_uncertainty.py" \
	"$root/tests/test_native_flow_control.py" \
	"$root/tests/test_anchor_recovery.py" \
	-q --timeout=20 --show-capture=no
