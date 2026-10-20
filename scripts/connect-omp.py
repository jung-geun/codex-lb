#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28,<1", "ruamel.yaml>=0.18,<0.19"]
# ///
"""Install only repository-owned OMP code; never fetch executable code."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
ROOTS = {
    "openai-codex-responses": {"/backend-api/codex"},
    "anthropic-messages": {"/"},
    "openai-completions": {"/v1"},
}
REQUIRED = {"id", "name", "api", "basePath", "reasoning", "input", "contextWindow", "maxTokens"}
OPTIONAL = {"thinking", "compat", "preferWebsockets", "remoteCompaction"}
EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
THINKING_BOOLS = {"supportsDisplay", "prefixBinding", "suppressWhenOff", "requiresEffort", "unverified"}
COMPAT_BOOLS = {
    "supportsStore", "supportsDeveloperRole", "supportsReasoningEffort", "supportsUsageInStreaming",
    "supportsStrictMode", "supportsToolChoice", "supportsParallelToolCalls",
}
COMPACTION_PATHS = {"/responses/compact", "/responses/compact/stream", "/responses/compact/v2"}


def origin(value: str) -> str:
    """Accept a TLS origin, or an existing native API root on that origin."""
    if not isinstance(value, str) or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Base URL must be a TLS gateway origin")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1", "/backend-api/codex"}):
        raise ValueError("Base URL must be a TLS gateway origin or native root")
    port = parsed.port
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise ValueError("Invalid gateway hostname")
    host = f"[{host}]" if ":" in host else host
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def env_key(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Key must be referenced by a named environment variable")
    value = os.environ.get(name, "")
    if not value.strip() or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Gateway key environment variable is empty or invalid")
    return value


def integer(value: object) -> bool:
    return type(value) is int and 0 < value <= 100_000_000


def record(value: object, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed:
        raise ValueError("Unsupported manifest fields")
    return value


def validate_manifest(payload: object) -> list[dict]:
    document = record(payload, {"schemaVersion", "models"}, {"schemaVersion", "models"})
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != VERSION:
        raise ValueError("Unsupported manifest version")
    models = document["models"]
    if not isinstance(models, list) or len(models) > 4096:
        raise ValueError("Manifest models must be a bounded array")
    seen = set()
    for model in models:
        record(model, REQUIRED | OPTIONAL, REQUIRED)
        model_id = model["id"]
        if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model_id):
            raise ValueError("Invalid model id")
        if model_id in seen:
            raise ValueError("Duplicate model id")
        seen.add(model_id)
        if not isinstance(model["name"], str) or not model["name"] or len(model["name"]) > 256 or any(ord(c) < 32 for c in model["name"]):
            raise ValueError("Invalid model name")
        api = model["api"]
        if not isinstance(api, str) or api not in ROOTS or not isinstance(model["basePath"], str) or model["basePath"] not in ROOTS[api]:
            raise ValueError("Unsupported native API or relative root")
        if type(model["reasoning"]) is not bool:
            raise ValueError("Invalid reasoning capability")
        inputs = model["input"]
        if not isinstance(inputs, list) or not inputs or any(x not in ("text", "image") for x in inputs) or len(set(inputs)) != len(inputs):
            raise ValueError("Invalid input capability")
        if not integer(model["contextWindow"]) or not integer(model["maxTokens"]):
            raise ValueError("Invalid token limits")
        if "preferWebsockets" in model and (type(model["preferWebsockets"]) is not bool or api != "openai-codex-responses"):
            raise ValueError("WebSockets are Codex-only")
        if "thinking" in model:
            thinking = record(model["thinking"], {"mode", "efforts", "defaultLevel", "effortMap", "effortBudgets"} | THINKING_BOOLS, {"mode", "efforts"})
            if not model["reasoning"] or not isinstance(thinking["mode"], str) or thinking["mode"] not in {"effort", "budget", "google-level", "anthropic-adaptive", "anthropic-budget-effort"}:
                raise ValueError("Invalid thinking mode")
            efforts = thinking["efforts"]
            if not isinstance(efforts, list) or not efforts or any(not isinstance(x, str) or x not in EFFORTS for x in efforts) or len(set(efforts)) != len(efforts):
                raise ValueError("Invalid thinking efforts")
            if "defaultLevel" in thinking and thinking["defaultLevel"] not in efforts:
                raise ValueError("Invalid default thinking effort")
            for field in THINKING_BOOLS & thinking.keys():
                if type(thinking[field]) is not bool:
                    raise ValueError("Invalid thinking flag")
            for field in ("effortMap", "effortBudgets"):
                if field in thinking:
                    values = record(thinking[field], EFFORTS)
                    for value in values.values():
                        valid = integer(value) if field == "effortBudgets" else isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value)
                        if not valid:
                            raise ValueError("Invalid thinking mapping")
        if "compat" in model:
            compat = record(model["compat"], COMPAT_BOOLS | {"maxTokensField"})
            if any(type(compat[k]) is not bool for k in COMPAT_BOOLS & compat.keys()):
                raise ValueError("Invalid compatibility flag")
            if "maxTokensField" in compat and (not isinstance(compat["maxTokensField"], str) or compat["maxTokensField"] not in {"max_tokens", "max_completion_tokens"}):
                raise ValueError("Invalid output token field")
        if "remoteCompaction" in model:
            compaction = record(model["remoteCompaction"], {"enabled", "api", "endpoint", "v2StreamingEnabled", "v2Endpoint", "streamingEndpoint"})
            if api != "openai-codex-responses" or not model_id.startswith("gpt-"):
                raise ValueError("Compaction is GPT-only")
            for field, value in compaction.items():
                if field in {"enabled", "v2StreamingEnabled"}:
                    valid = type(value) is bool
                elif field == "api":
                    valid = value == "openai-codex-responses"
                else:
                    valid = isinstance(value, str) and value in COMPACTION_PATHS
                if not valid:
                    raise ValueError("Invalid remote compaction contract")
    return copy.deepcopy(models)


def fetch_manifest(base_origin: str, key: str, client: httpx.Client | None = None) -> list[dict]:
    def fetch(connection: httpx.Client) -> list[dict]:
        with connection.stream("GET", base_origin + "/v1/omp/models", headers={"Authorization": "Bearer " + key}, follow_redirects=False) as response:
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise ValueError("Manifest authentication or origin verification failed")
            if response.status_code != 200:
                raise ConnectionError("Authenticated manifest discovery unavailable")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_MANIFEST_BYTES:
                    raise ValueError("Manifest exceeds size limit")
            try:
                return validate_manifest(json.loads(data))
            except (TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError("Invalid model manifest") from exc
    if client is not None:
        return fetch(client)
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as connection:
        return fetch(connection)


def merge_provider(document: dict, base_origin: str, key_env: str, models: list[dict] | None) -> dict:
    providers = document.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Invalid providers configuration")
    provider = providers.setdefault("codex-lb", {})
    if not isinstance(provider, dict):
        raise ValueError("Invalid codex-lb configuration")
    # YAML anchors may share this mapping with unrelated providers/settings.
    # Isolate only the owned provider before merging so aliases cannot widen edits.
    provider = copy.deepcopy(provider)
    providers["codex-lb"] = provider
    if "baseUrl" in provider and origin(provider["baseUrl"]) != base_origin:
        raise ValueError("Existing codex-lb origin differs; explicit migration is required")
    if "apiKey" in provider and provider["apiKey"] != key_env:
        raise ValueError("Use the preserved codex-lb apiKey environment reference")
    provider.setdefault("baseUrl", base_origin + "/backend-api/codex")
    provider.setdefault("apiKey", key_env)
    existing = provider.setdefault("models", [])
    overrides = provider.setdefault("modelOverrides", {})
    if not isinstance(existing, list) or not isinstance(overrides, dict):
        raise ValueError("Invalid static model configuration")
    for model_id, override in overrides.items():
        if not isinstance(model_id, str) or not isinstance(override, dict) or ("remoteCompaction" in override and not isinstance(override["remoteCompaction"], dict)):
            raise ValueError("Invalid model override configuration")
        if not model_id.startswith("gpt-"):
            override.pop("remoteCompaction", None)
    discovery = provider.get("discovery")
    if isinstance(discovery, dict) and discovery.get("type") == "openai-models-list":
        del provider["discovery"]
    compaction = provider.pop("remoteCompaction", None)
    if compaction is not None and not isinstance(compaction, dict):
        raise ValueError("Invalid provider compaction configuration")
    # A provider-wide auth override must not turn Claude into an API-key materialization.
    # Do not silently rewrite a user's explicit non-native auth policy.
    if provider.get("auth") not in (None, "oauth"):
        raise ValueError("Remove codex-lb auth override explicitly to permit Claude OAuth shaping")
    known = {}
    for model in existing:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            raise ValueError("Invalid static model")
        if "remoteCompaction" in model and not isinstance(model["remoteCompaction"], dict):
            raise ValueError("Invalid model compaction configuration")
        if model["id"] in known:
            raise ValueError("Duplicate static model id")
        known[model["id"]] = model
    for row in models or []:
        if row["id"] not in known:
            model = copy.deepcopy(row)
            model["baseUrl"] = base_origin + model.pop("basePath")
            for field in ("endpoint", "v2Endpoint", "streamingEndpoint"):
                if field in model.get("remoteCompaction", {}):
                    model["remoteCompaction"][field] = base_origin + "/backend-api/codex" + model["remoteCompaction"][field]
            existing.append(model)
            known[row["id"]] = model
    if not existing:
        raise ValueError("Discovery unavailable and no static fallback exists")
    for model_id, model in known.items():
        api = model.get("api", provider.get("api"))
        # Existing per-model URLs must retain native roots on this gateway too.
        root = model.get("baseUrl", provider["baseUrl"])
        if not isinstance(api, str) or api not in ROOTS or origin(root) != base_origin or (urlsplit(root).path.rstrip("/") or "/") not in ROOTS[api]:
            raise ValueError("Invalid native static fallback")
        if model_id.startswith("gpt-") and api == "openai-codex-responses":
            # Native GPT may fall back to an HTTP-only paid API on this gateway.
            # OMP treats an in-band WebSocket transport error as terminal.
            model["preferWebsockets"] = False
            override = overrides.setdefault(model_id, {})
            override["preferWebsockets"] = False
            inherited = copy.deepcopy(compaction or {})
            inherited.update(model.pop("remoteCompaction", {}))
            if inherited:
                inherited.update(override.get("remoteCompaction", {}))
                override["remoteCompaction"] = inherited
        else:
            model.pop("remoteCompaction", None)
            if model_id in overrides:
                overrides[model_id].pop("remoteCompaction", None)
    return document


def safe_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    for part in reversed([path, *path.parents]):
        if not part.exists() and not part.is_symlink():
            part.mkdir(mode=0o700)
        if not stat.S_ISDIR(part.lstat().st_mode):
            raise ValueError("Installation paths must not contain symlinks")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise ValueError("Agent directory must be owned by this user and not writable by others")
    return path


def regular_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing symlink destination")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Destination must be a private user-owned regular file")


def atomic_write(path: Path, data: bytes, backup: bool = False) -> None:
    regular_file(path)
    if path.exists() and path.read_bytes() == data:
        return
    if backup and path.exists():
        fd, _ = tempfile.mkstemp(prefix="." + path.name + ".backup-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        regular_file(path)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def installation_lock(directory: Path):
    import fcntl
    path = directory / ".codex-lb-install.lock"
    regular_file(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def install(agent_dir: Path, base_url: str, key_env: str, *, client: httpx.Client | None = None) -> bool:
    base_origin = origin(base_url)
    key = env_key(key_env)  # Fail before any HTTP request or file creation.
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    # Fetch/validate first: an untrusted manifest never changes a profile.
    try:
        models = fetch_manifest(base_origin, key, client)
    except (httpx.TransportError, ConnectionError):
        models = None
    directory = safe_directory(agent_dir)
    with installation_lock(directory):
        config = directory / "models.yml"
        if not config.exists() and not config.is_symlink() and ((directory / "models.yaml").exists() or (directory / "models.yaml").is_symlink()):
            config = directory / "models.yaml"
        regular_file(config)
        document = yaml.load(config.read_text()) if config.exists() else {}
        if document is None:
            document = {}
        if not isinstance(document, dict):
            raise ValueError("Models config must be a mapping")
        document = merge_provider(document, base_origin, key_env, models)
        stream = io.StringIO()
        yaml.dump(document, stream)
        extensions = safe_directory(directory / "extensions")
        if (extensions / "codex-lb.ts").exists() or (extensions / "codex-lb.ts").is_symlink():
            raise ValueError("An unversioned codex-lb extension exists; remove it explicitly first")
        extension = extensions / "codex-lb-v1.ts"
        settings = extensions / "codex-lb-v1.json"
        regular_file(extension)
        regular_file(settings)
        source = Path(__file__).resolve().parents[1] / "integrations" / "omp" / "codex-lb.ts"
        # Keep helper modules outside OMP's top-level extension scan.
        updater_dir = safe_directory(extensions / ".codex-lb")
        updater = updater_dir / "update-check.ts"
        package = updater_dir / "package.json"
        regular_file(updater)
        regular_file(package)
        source_text = source.read_text(encoding="utf-8")
        if source_text.count('from "./update-check"') != 1:
            raise ValueError("Expected exactly one local updater import")
        extension_bytes = source_text.replace('from "./update-check"', 'from "./.codex-lb/update-check"').encode("utf-8")
        settings_bytes = (json.dumps({"schemaVersion": VERSION, "baseOrigin": base_origin, "keyEnv": key_env}, indent=2) + "\n").encode()
        # Install prerequisites before config cutover; every replacement is private and atomic.
        atomic_write(settings, settings_bytes, backup=True)
        atomic_write(extensions / "codex-defaults.json", source.with_name("codex-defaults.json").read_bytes(), backup=True)
        atomic_write(updater, source.with_name("update-check.ts").read_bytes(), backup=True)
        atomic_write(package, source.with_name("package.json").read_bytes(), backup=True)
        atomic_write(extension, extension_bytes, backup=True)
        atomic_write(config, stream.getvalue().encode(), backup=True)
    return models is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-env", required=True)
    parser.add_argument("--agent-dir", type=Path, default=Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.omp/agent")), help="Explicit active agent directory, required for a named profile")
    args = parser.parse_args()
    try:
        online = install(args.agent_dir, args.base_url, args.key_env)
    except (ValueError, OSError, httpx.HTTPError, YAMLError):
        print("OMP installation refused; check TLS origin, nonempty key env, manifest, and profile preservation constraints.", file=sys.stderr)
        return 1
    print("Installed local codex-lb v1 extension; " + ("authenticated manifest verified." if online else "discovery unavailable; existing static fallback preserved."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
