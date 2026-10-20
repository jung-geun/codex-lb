#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
	echo "usage: $0 /path/to/session.jsonl" >&2
	exit 2
fi

source_session="$1"
if [[ ! -f "$source_session" ]]; then
	echo "session does not exist: $source_session" >&2
	exit 2
fi

python3 - "$source_session" <<'PY'
import json
import sys

found = []
with open(sys.argv[1], encoding="utf-8") as stream:
    for line_number, line in enumerate(stream, 1):
        entry = json.loads(line)
        if entry.get("type") != "compaction":
            continue
        history = (
            (entry.get("preserveData") or {})
            .get("openaiRemoteCompaction", {})
            .get("replacementHistory", [])
        )
        for item in history:
            if isinstance(item, dict) and item.get("type") in {"compaction", "compaction_summary"} and "status" in item:
                found.append((line_number, item.get("id"), item.get("status")))
if not found:
    raise SystemExit("session has no status-bearing compaction replay item; choose a previously affected session")
print(f"poisoned replay item: {found[-1]}")
PY

tmpdir="$(mktemp -d /tmp/codex-lb-omp-smoke.XXXXXX)"
trap 'rm -rf "$tmpdir"' EXIT
probe_session="$tmpdir/session.jsonl"
cp "$source_session" "$probe_session"

omp --resume "$probe_session" \
	--model codex-lb/gpt-5.6-sol \
	--thinking low \
	--no-tools \
	--no-title \
	--max-time 3m \
	-p 'Reply with exactly OMP_CODEXLB_WS_OK and nothing else.'

python3 - "$probe_session" <<'PY'
import json
import sys

match = None
with open(sys.argv[1], encoding="utf-8") as stream:
    for line in stream:
        entry = json.loads(line)
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        text = "".join(
            part.get("text", "")
            for part in message.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        if "OMP_CODEXLB_WS_OK" in text:
            match = message
if match is None:
    raise SystemExit("OMP sentinel response was not recorded")
if match.get("provider") != "codex-lb" or match.get("model") != "gpt-5.6-sol":
    raise SystemExit(f"wrong route: provider={match.get('provider')} model={match.get('model')}")
if match.get("stopReason") != "stop":
    raise SystemExit(f"turn did not finish normally: {match.get('stopReason')}")
print(
    "omp replay ok: "
    f"provider={match['provider']} model={match['model']} "
    f"stopReason={match['stopReason']}"
)
PY
