"""Synthetic-only CLI boundary regressions; never read a real user's credentials."""
from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connect = load_script("connect-omp")
importer = load_script("import-claude")
ORIGIN = "https://gateway.example"
KEY_ENV = "SYNTHETIC_CODEX_KEY"


def row(model_id="gpt-test", api="openai-codex-responses", base="/backend-api/codex"):
    return {"id": model_id, "name": model_id, "api": api, "basePath": base,
            "reasoning": False, "input": ["text"], "contextWindow": 128000, "maxTokens": 16384}


def manifest():
    return {"schemaVersion": 1, "models": [row(), row("claude-test", "anthropic-messages", "/"), row("glm-test", "openai-completions", "/v1")]}


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path.resolve() / "profiles" / "named" / "agent"
    directory.mkdir(parents=True, mode=0o700)
    # Eight existing GPT fallbacks, a real preservation target, not a token echo.
    names = ["gpt-6.1-sol", *[f"gpt-preserved-{n}" for n in range(7)]]
    models = "".join(f"      - id: {name}\n        name: '{name}'\n        reasoning: true\n        input: [text, image]\n        contextWindow: 200000\n        maxTokens: 32768\n" for name in names)
    config = directory / "models.yml"
    config.write_text("# keep this comment\nproviders:\n  unrelated:\n    apiKey: OTHER_KEY\n    discovery:\n      type: openai-models-list\n    headers:\n      X-Private: keep\n  codex-lb:\n    baseUrl: https://gateway.example/backend-api/codex\n    apiKey: SYNTHETIC_CODEX_KEY\n    api: openai-codex-responses\n    discovery:\n      type: openai-models-list\n    remoteCompaction:\n      enabled: true\n      api: openai-codex-responses\n    modelOverrides:\n      gpt-6.1-sol:\n        contextWindow: 500000 # fixed window\n    models:\n" + models)
    roles = directory / "config.yml"
    roles.write_text("modelRoles:\n  default: codex-lb/gpt-6.1-sol\n  slow: unrelated/model\n")
    return directory


def client_for(payload, requests=None, status=200):
    def handle(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)


def test_installer_preserves_eight_fallbacks_overrides_roles_comments_and_other_provider(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-gateway-secret")
    before = (profile / "models.yml").read_bytes()
    roles = (profile / "config.yml").read_bytes()
    requests = []
    with client_for(manifest(), requests) as client:
        assert connect.install(profile, ORIGIN, KEY_ENV, client=client)
        installed = (profile / "models.yml").read_bytes()
        assert connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert (profile / "models.yml").read_bytes() == installed
    assert (profile / "config.yml").read_bytes() == roles
    yaml = YAML(typ="rt")
    old = yaml.load(before)
    new = yaml.load(installed)
    assert new["providers"]["unrelated"] == old["providers"]["unrelated"]
    provider = new["providers"]["codex-lb"]
    assert [{key: value for key, value in model.items() if key != "preferWebsockets"} for model in provider["models"][:8]] == old["providers"]["codex-lb"]["models"]
    assert provider["modelOverrides"]["gpt-6.1-sol"]["contextWindow"] == 500000
    assert provider["apiKey"] == KEY_ENV
    assert "remoteCompaction" not in provider and "discovery" not in provider
    assert all(provider["modelOverrides"][model["id"]]["remoteCompaction"]["enabled"] for model in provider["models"] if model["id"].startswith("gpt-"))
    assert all("remoteCompaction" not in model for model in provider["models"] if not model["id"].startswith("gpt-"))
    assert b"# keep this comment" in installed and b"# fixed window" in installed
    backups = list(profile.glob(".models.yml.backup-*"))
    assert backups and any(path.read_bytes() == before for path in backups)
    for path in [profile / "models.yml", backups[0], profile / "extensions/codex-lb-v1.json", profile / "extensions/codex-lb-v1.ts", profile / "extensions/.codex-lb/update-check.ts", profile / "extensions/.codex-lb/package.json"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"synthetic-gateway-secret" not in installed
    entry = (profile / "extensions/codex-lb-v1.ts").read_text()
    assert entry.count('from "./.codex-lb/update-check"') == 1
    assert 'from "./update-check"' not in entry
    source = Path(connect.__file__).resolve().parents[1] / "integrations/omp"
    assert (profile / "extensions/.codex-lb/update-check.ts").read_bytes() == (source / "update-check.ts").read_bytes()
    assert (profile / "extensions/.codex-lb/package.json").read_bytes() == (source / "package.json").read_bytes()
    assert not (profile / "extensions/update-check.ts").exists()
    assert not (profile / "extensions/package.json").exists()
    assert {path.name for path in (profile / "extensions").glob("*.ts")} == {"codex-lb-v1.ts"}


@pytest.mark.parametrize("mutation", [
    {"basePath": "https://attacker.example/"}, {"basePath": "//attacker.example/"},
    {"basePath": "/v1/../evil"}, {"basePath": "/v1?token=x"}, {"basePath": "/%2f%2fattacker"},
    {"basePath": []}, {"api": "shell"}, {"headers": {"Authorization": "steal"}},
    {"apiKey": "!touch /tmp/should-not-run"}, {"compat": {"extraBody": {"execute": "bad"}}},
    {"reasoning": "true"}, {"contextWindow": True}, {"input": ["audio"]},
    {"thinking": {"mode": "budget", "efforts": ["high"]}},
    {"remoteCompaction": {"endpoint": "https://attacker.example/compact"}},
])
def test_malicious_manifest_leaves_profile_untouched(profile, monkeypatch, mutation):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    before = {p.name: p.read_bytes() for p in profile.iterdir()}
    payload = manifest()
    payload["models"][0].update(mutation)
    with client_for(payload) as client, pytest.raises(ValueError):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert {p.name: p.read_bytes() for p in profile.iterdir()} == before


@pytest.mark.parametrize("payload", [{"schemaVersion": True, "models": [row()]}, {"schemaVersion": 2, "models": [row()]}, {"schemaVersion": 1, "models": [row(), row()]}])
def test_version_and_duplicate_model_contract(payload):
    with pytest.raises(ValueError):
        connect.validate_manifest(payload)


@pytest.mark.parametrize("value", [None, "", " ", "bad\nkey"])
def test_empty_key_fails_before_network_or_profile_creation(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(KEY_ENV, value)
    requests = []
    directory = tmp_path.resolve() / "never-created"
    with client_for(manifest(), requests) as client, pytest.raises(ValueError):
        connect.install(directory, ORIGIN, KEY_ENV, client=client)
    assert requests == [] and not directory.exists()


def test_unavailable_discovery_keeps_native_offline_models(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    with client_for({}, status=503) as client:
        assert connect.install(profile, ORIGIN, KEY_ENV, client=client) is False
    provider = YAML().load((profile / "models.yml").read_text())["providers"]["codex-lb"]
    assert len(provider["models"]) == 8
    assert provider["modelOverrides"]["gpt-6.1-sol"]["contextWindow"] == 500000
    assert (profile / "extensions/codex-lb-v1.ts").exists()


@pytest.mark.parametrize("status", [301, 302, 401, 403])
def test_redirect_or_auth_rejection_is_not_an_offline_install(profile, monkeypatch, status):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    before = (profile / "models.yml").read_bytes()
    with client_for({}, status=status) as client, pytest.raises(ValueError):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert (profile / "models.yml").read_bytes() == before
    assert not (profile / "extensions").exists()


def test_symlink_extension_cannot_overwrite_unrelated_file(profile, tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    extensions = profile / "extensions"
    extensions.mkdir(mode=0o700)
    unrelated = tmp_path / "private.ts"
    unrelated.write_text("unrelated code")
    (extensions / "codex-lb-v1.ts").symlink_to(unrelated)
    before = (profile / "models.yml").read_bytes()
    with client_for(manifest()) as client, pytest.raises(ValueError):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert unrelated.read_text() == "unrelated code"
    assert (profile / "models.yml").read_bytes() == before


def test_other_discovery_type_is_not_removed():
    document = {"providers": {"codex-lb": {"discovery": {"type": "custom-owned"}}}}
    connect.merge_provider(document, ORIGIN, KEY_ENV, connect.validate_manifest(manifest()))
    assert document["providers"]["codex-lb"]["discovery"] == {"type": "custom-owned"}


def test_yaml_aliases_do_not_widen_provider_changes():
    yaml = YAML(typ="rt")
    document = yaml.load("providers:\n  unrelated: &shared\n    baseUrl: https://gateway.example/backend-api/codex\n    apiKey: SYNTHETIC_CODEX_KEY\n    discovery: {type: openai-models-list}\n  codex-lb: *shared\n")
    connect.merge_provider(document, ORIGIN, KEY_ENV, connect.validate_manifest(manifest()))
    assert document["providers"]["unrelated"]["discovery"]["type"] == "openai-models-list"
    assert "models" not in document["providers"]["unrelated"]
    assert "discovery" not in document["providers"]["codex-lb"]


@pytest.mark.parametrize("field,value", [("baseUrl", "https://other.example/backend-api/codex"), ("apiKey", "OTHER_KEY"), ("auth", "apiKey")])
def test_conflicting_provider_config_is_not_silently_overwritten(profile, monkeypatch, field, value):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    yaml = YAML(typ="rt")
    path = profile / "models.yml"
    document = yaml.load(path.read_text())
    document["providers"]["codex-lb"][field] = value
    with path.open("w") as stream:
        yaml.dump(document, stream)
    before = path.read_bytes()
    with client_for(manifest()) as client, pytest.raises(ValueError):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert path.read_bytes() == before
    assert not (profile / "extensions").exists()


def test_foreign_static_model_root_is_rejected_without_overwrite(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    yaml = YAML(typ="rt")
    path = profile / "models.yml"
    document = yaml.load(path.read_text())
    document["providers"]["codex-lb"]["models"][0]["baseUrl"] = "https://attacker.example/backend-api/codex"
    with path.open("w") as stream:
        yaml.dump(document, stream)
    before = path.read_bytes()
    with client_for(manifest()) as client, pytest.raises(ValueError):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert path.read_bytes() == before


def credential_record():
    return {"accessToken": "synthetic-access", "refreshToken": "synthetic-refresh", "expiresAt": 1890000000000, "scopes": ["user:inference"]}




def test_mac_keychain_failure_does_not_import_stale_file(tmp_path, monkeypatch):
    directory = tmp_path / ".claude"
    directory.mkdir()
    (directory / ".credentials.json").write_text(json.dumps(credential_record()))
    monkeypatch.setattr(importer.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 1, b"", b"private upstream text"))
    with pytest.raises(ValueError, match="Keychain"):
        importer.read_local_credentials("darwin", tmp_path)


def test_linux_safe_json_parser_never_executes_or_imports_settings(tmp_path):
    tmp_path.chmod(0o700)
    directory = tmp_path / ".claude"
    directory.mkdir(mode=0o700)
    path = directory / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": credential_record(), "unrelated": {"secret": "do-not-upload"}}))
    path.chmod(0o600)
    assert importer.read_local_credentials("linux", tmp_path) == credential_record()
    path.write_text("__import__('os').system('echo bad')")
    with pytest.raises(ValueError, match="JSON"):
        importer.read_local_credentials("linux", tmp_path)
    path.write_text(json.dumps(credential_record()))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        importer.read_local_credentials("linux", tmp_path)


def test_linux_symlink_credential_is_rejected(tmp_path):
    tmp_path.chmod(0o700)
    directory = tmp_path / ".claude"
    directory.mkdir(mode=0o700)
    target = tmp_path / "other-user.json"
    target.write_text(json.dumps(credential_record()))
    target.chmod(0o600)
    (directory / ".credentials.json").symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        importer.read_local_credentials("linux", tmp_path)


def test_upload_requires_tls_source_session_csrf_and_acknowledgement(capsys):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"accessToken": "echo-must-not-print"})
    with httpx.Client(transport=httpx.MockTransport(handle), cookies={importer.COOKIE: "synthetic-session"}) as client:
        for origin, source, ack in [(ORIGIN, "source-1", False), ("http://gateway.example", "source-1", True), (ORIGIN, "../other", True)]:
            with pytest.raises(ValueError):
                importer.upload_credentials(client, origin, source, credential_record(), single_writer_confirmed=ack)
        assert requests == []
        importer.upload_credentials(client, ORIGIN, "source-1", credential_record(), single_writer_confirmed=True)
    request = requests[0]
    assert request.url.path == "/api/model-sources/source-1/credentials/import"
    assert request.headers["origin"] == ORIGIN and request.headers["sec-fetch-site"] == "same-origin"
    assert importer.COOKIE in request.headers["cookie"] and "authorization" not in request.headers
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_failure_body_never_leaks_credentials(capsys):
    with client_for({"error": "synthetic-access synthetic-refresh synthetic-session"}, status=403) as client:
        with pytest.raises(ValueError) as failure:
            importer.upload_credentials(client, ORIGIN, "source-1", credential_record(), single_writer_confirmed=True)
    assert "synthetic-" not in str(failure.value)
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_password_login_totp_uses_existing_session_mechanisms(monkeypatch, capsys):
    prompts = iter(["fixture-user", "fixture-password", "123456"])
    monkeypatch.setattr(importer, "hidden", lambda prompt, **kwargs: next(prompts))
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/password/login"):
            return httpx.Response(200, json={"authenticated": False, "totpRequiredOnLogin": True}, headers={"set-cookie": importer.COOKIE + "=fixture-session; Path=/; Secure; HttpOnly"})
        assert request.url.path.endswith("/totp/verify")
        assert importer.COOKIE in request.headers["cookie"]
        return httpx.Response(200, json={"authenticated": True})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        importer.authenticate(client, ORIGIN, "password")
    assert [request.url.path for request in requests] == ["/api/dashboard-auth/password/login", "/api/dashboard-auth/totp/verify"]
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_session_authentication_refuses_unauthenticated_dashboard(monkeypatch):
    monkeypatch.setattr(importer, "hidden", lambda prompt: "fixture-session")
    with client_for({"authenticated": False}) as client, pytest.raises(ValueError, match="authenticated"):
        importer.authenticate(client, ORIGIN, "session")


def test_no_single_writer_ack_does_not_even_read_local_credentials(monkeypatch, capsys):
    monkeypatch.setattr(importer.sys, "argv", ["import-claude.py", "--base-url", ORIGIN, "--source-id", "source-1"])
    def forbidden():
        raise AssertionError("Must not read credentials without acknowledgement")
    monkeypatch.setattr(importer, "read_local_credentials", forbidden)
    assert importer.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "single-writer" in captured.err


def test_noninteractive_getpass_never_falls_back_to_echo(monkeypatch):
    import warnings
    def fallback(prompt):
        warnings.warn("cannot hide input", importer.getpass.GetPassWarning)
        raise AssertionError("Never reach an echoing fallback")
    monkeypatch.setattr(importer.getpass, "getpass", fallback)
    with pytest.raises(importer.getpass.GetPassWarning):
        importer.hidden("Dashboard password: ")


def test_models_yaml_and_named_profile_are_supported_without_creating_yml(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    config = profile / "models.yml"
    config.rename(profile / "models.yaml")
    with client_for(manifest()) as client:
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert not config.exists()
    assert (profile / "models.yaml").exists()
    assert list(profile.glob(".models.yaml.backup-*"))
    settings = json.loads((profile / "extensions/codex-lb-v1.json").read_bytes())
    assert settings == {"schemaVersion": 1, "baseOrigin": ORIGIN, "keyEnv": KEY_ENV}


def test_dangling_models_symlink_is_not_bypassed_with_yaml(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    config = profile / "models.yml"
    config.rename(profile / "models.yaml")
    config.symlink_to(profile / "missing-private-config")
    before = (profile / "models.yaml").read_bytes()
    with client_for(manifest()) as client, pytest.raises(ValueError, match="symlink"):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert (profile / "models.yaml").read_bytes() == before
    assert config.is_symlink() and not config.exists()


def test_manifest_compaction_stays_in_gpt_overrides(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    payload = manifest()
    payload["models"][0]["remoteCompaction"] = {"enabled": True, "api": "openai-codex-responses", "endpoint": "/responses/compact"}
    with client_for(payload) as client:
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    provider = YAML().load((profile / "models.yml").read_text())["providers"]["codex-lb"]
    assert all("remoteCompaction" not in model for model in provider["models"])
    compaction = provider["modelOverrides"]["gpt-test"]["remoteCompaction"]
    assert compaction["endpoint"] == ORIGIN + "/backend-api/codex/responses/compact"
    assert "claude-test" not in provider["modelOverrides"]


def test_anthropic_v1_root_is_rejected_before_profile_mutation(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    payload = manifest()
    payload["models"][1]["basePath"] = "/v1"
    before = (profile / "models.yml").read_bytes()
    with client_for(payload) as client, pytest.raises(ValueError):
        connect.install(profile, ORIGIN, KEY_ENV, client=client)
    assert (profile / "models.yml").read_bytes() == before
    assert not (profile / "extensions").exists()


def test_empty_eligible_roster_does_not_destroy_existing_fallbacks(profile, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "synthetic-key")
    payload = {"schemaVersion": 1, "models": []}
    with client_for(payload) as client:
        assert connect.install(profile, ORIGIN, KEY_ENV, client=client)
    provider = YAML().load((profile / "models.yml").read_text())["providers"]["codex-lb"]
    assert len(provider["models"]) == 8
    assert provider["modelOverrides"]["gpt-6.1-sol"]["contextWindow"] == 500000
