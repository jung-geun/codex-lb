from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core.openai.model_registry import ReasoningLevel, get_model_registry
from app.db.models import ApiKey, ModelSource, ModelSourceModel, ModelSourceQuotaWindow
from app.db.session import get_background_session
from app.modules.proxy import api as proxy_api
from app.modules.proxy.omp_models_api import OmpModel

pytestmark = pytest.mark.integration


async def _source(
    model: str,
    *,
    native: bool,
    context: int | None = 200_000,
    output: int | None = 32_000,
    used: float = 0.1,
    raw: dict | None = None,
) -> str:
    now = datetime.now(UTC)
    source_id = f"source-{model}"
    source = ModelSource(
        id=source_id,
        name=model,
        kind="anthropic_compatible" if native else "openai_compatible",
        base_url="https://upstream-private.example.invalid",
        api_key_encrypted=b"encrypted-fixture",
        provider="claude" if native else "zai",
        auth_type="api_key",
        billing_mode="subscription" if native else "coding_plan",
        entitlement_status="verified",
        entitlement_verified_at=now,
        extra_usage_enabled=False,
        quota_checked_at=now,
        supports_messages=True,
        supports_chat_completions=not native,
        supports_responses=native,
        models=[
            ModelSourceModel(
                model=model,
                context_window=context,
                max_output_tokens=output,
                subscription_eligible=True,
                supports_tools=True,
                supports_vision=True,
                raw_metadata_json=json.dumps(raw or {}),
            )
        ],
        quota_windows=[
            ModelSourceQuotaWindow(
                window_key="weekly",
                used_ratio=used,
                observed_at=now,
                resets_at=now + timedelta(days=1),
            )
        ],
    )
    async with get_background_session() as session:
        session.add(source)
        await session.commit()
    return source_id


async def _gpt() -> None:
    registry = get_model_registry()
    template = next(iter(registry.get_models_with_fallback().values()))
    model = replace(
        template,
        slug="gpt-fixture",
        display_name="Live authorized GPT",
        context_window=272_000,
        prefer_websockets=True,
        supported_in_api=True,
        supported_reasoning_levels=(
            ReasoningLevel(effort="low", description="Low"),
            ReasoningLevel(effort="high", description="High"),
        ),
        default_reasoning_level="high",
        raw={
            "visibility": "list",
            "shell_type": "shell_command",
            "max_context_window": 872_000,
            "max_output_tokens": 128_000,
            "remoteCompaction": {
                "enabled": True,
                "api": "openai-codex-responses",
                "endpoint": "/responses/compact",
                "v2StreamingEnabled": True,
                "v2Endpoint": "/responses/compact/v2",
                "upstreamToken": "never-export-me",
            },
        },
    )
    await registry.update({"plus": [model]})


async def _key(async_client, **settings) -> str:
    response = await async_client.put("/api/settings", json={"apiKeyAuthEnabled": True})
    assert response.status_code == 200
    response = await async_client.post("/api/api-keys/", json={"name": "manifest fixture key", **settings})
    assert response.status_code == 200
    return response.json()["key"]


@pytest.mark.asyncio
async def test_manifest_consumers_receive_native_roots_limits_and_safe_metadata(async_client, monkeypatch) -> None:
    await _gpt()
    await _source(
        "claude-fixture",
        native=True,
        raw={
            "supports_reasoning": True,
            "supported_reasoning_levels": ["low", "high"],
            "thinking": {
                "mode": "anthropic-adaptive",
                "efforts": ["low", "high"],
                "defaultLevel": "high",
                "headers": {"secret": "never-export-me"},
            },
            "compat": {"supportsToolChoice": True, "headers": {"secret": "never-export-me"}},
            "source_request_overrides": {"headers": {"authorization": "never-export-me"}},
            "remoteCompaction": {"enabled": True},
        },
    )
    await _source(
        "glm-fixture",
        native=False,
        raw={
            "supports_reasoning": True,
            "supported_reasoning_levels": ["low", "high"],
            "compat": {"maxTokensField": "max_tokens", "supportsReasoningEffort": True},
        },
    )

    async def overrides():
        return {"gpt-fixture": 500_000, "claude-fixture": 180_000}

    monkeypatch.setattr(proxy_api, "_effective_context_window_overrides", overrides)
    response = await async_client.get("/v1/omp/models")
    assert response.status_code == 200
    document = response.json()
    assert set(document) == {"schemaVersion", "models"}
    assert document["schemaVersion"] == 1
    rows = {row["id"]: row for row in document["models"]}
    assert {"gpt-fixture", "claude-fixture", "glm-fixture"} <= rows.keys()
    # Consume the actual endpoint result, rather than inspect implementation text.
    for row in document["models"]:
        assert OmpModel.model_validate(row).model_dump(exclude_none=True) == row
        assert all(value is not None for value in row.values())
    gpt, claude, glm = (rows[name] for name in ("gpt-fixture", "claude-fixture", "glm-fixture"))
    assert (gpt["api"], gpt["basePath"]) == ("openai-codex-responses", "/backend-api/codex")
    assert gpt["contextWindow"] == 500_000
    assert gpt["preferWebsockets"] is True
    assert gpt["remoteCompaction"]["v2Endpoint"] == "/responses/compact/v2"
    assert (claude["api"], claude["basePath"]) == ("anthropic-messages", "/")
    assert claude["contextWindow"] == 180_000
    assert claude["thinking"] == {"mode": "anthropic-adaptive", "efforts": ["low", "high"], "defaultLevel": "high"}
    assert (glm["api"], glm["basePath"]) == ("openai-completions", "/v1")
    assert glm["thinking"] == {"mode": "effort", "efforts": ["low", "high"]}
    assert "remoteCompaction" not in claude and "remoteCompaction" not in glm
    assert "preferWebsockets" not in claude and "preferWebsockets" not in glm
    assert "never-export-me" not in response.text and "upstream-private" not in response.text
    assert "source_id" not in response.text and "api_key" not in response.text


@pytest.mark.asyncio
async def test_catalog_native_protocols_and_codex_exclusion_agree_with_manifest(async_client) -> None:
    await _gpt()
    await _source("claude-fixture", native=True)
    await _source("glm-fixture", native=False)
    response = await async_client.get("/v1/models")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    assert rows["claude-fixture"]["api_types"] == ["messages"]
    assert rows["glm-fixture"]["api_types"] == ["chat_completions", "messages"]
    for path in ("/backend-api/codex/models", "/v1/models?client_version=0.150.0"):
        response = await async_client.get(path)
        assert response.status_code == 200
        assert "claude-fixture" not in {row["slug"] for row in response.json()["models"]}


@pytest.mark.asyncio
async def test_manifest_requires_true_limits_but_retains_exhausted_verified_models(async_client) -> None:
    await _source("claude-no-context", native=True, context=None)
    await _source("claude-no-output", native=True, output=None)
    await _source("claude-exhausted", native=True, used=1.0)
    await _source("glm-eligible", native=False)
    response = await async_client.get("/v1/omp/models")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["models"]}
    assert "glm-eligible" in ids
    assert "claude-exhausted" in ids
    assert not {"claude-no-context", "claude-no-output"} & ids
    refused = await async_client.post("/v1/messages", json={
        "model": "claude-exhausted", "max_tokens": 1,
        "messages": [{"role": "user", "content": "Do not dispatch"}],
    })
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "permission_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["disabled", "stale", "unverified"])
async def test_exhaustion_discovery_exception_does_not_bypass_other_source_gates(async_client, state) -> None:
    from app.modules.model_sources.repository import ModelSourcesRepository

    source_id = await _source("claude-gated", native=True, used=1.0)
    async with get_background_session() as session:
        source = await ModelSourcesRepository(session).get_by_id(source_id)
        assert source is not None
        if state == "disabled":
            source.is_enabled = False
        elif state == "stale":
            source.quota_checked_at = datetime.now(UTC) - timedelta(minutes=6)
        else:
            source.entitlement_status = "unverified"
        await session.commit()
    response = await async_client.get("/v1/omp/models")
    assert response.status_code == 200
    assert "claude-gated" not in {row["id"] for row in response.json()["models"]}


@pytest.mark.asyncio
async def test_manifest_respects_exact_scope_billing_and_enforced_model(async_client) -> None:
    await _gpt()
    claude = await _source("claude-fixture", native=True)
    await _source("glm-fixture", native=False)
    key = await _key(
        async_client,
        allowedModels=["claude-fixture", "glm-fixture"],
        enforcedModel="claude-fixture",
        billingPolicy="subscription_only",
        sourceAssignmentScopeEnabled=True,
        assignedSourceIds=[claude],
    )
    response = await async_client.get("/v1/omp/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["models"]] == ["claude-fixture"]


@pytest.mark.asyncio
async def test_manifest_empty_source_scope_does_not_widen_or_use_static_gpt(async_client) -> None:
    await _source("claude-fixture", native=True)
    key = await _key(async_client, allowedModels=["claude-fixture"], assignedSourceIds=[])
    # A formerly scoped key can retain an empty scope after source deletion.
    # The public legacy [] input means unrestricted; do not invent an API flag.
    async with get_background_session() as session:
        from sqlalchemy import select

        row = (await session.execute(select(ApiKey).where(ApiKey.name == "manifest fixture key"))).scalar_one()
        row.source_assignment_scope_enabled = True
        await session.commit()
    response = await async_client.get("/v1/omp/models", headers={"x-api-key": key})
    assert response.status_code == 200
    assert response.json() == {"schemaVersion": 1, "models": []}


@pytest.mark.asyncio
async def test_manifest_keeps_gpt_precedence_over_colliding_source(async_client) -> None:
    await _gpt()
    await _source("gpt-fixture", native=True, context=100_000)
    response = await async_client.get("/v1/omp/models")
    assert response.status_code == 200
    rows = [row for row in response.json()["models"] if row["id"] == "gpt-fixture"]
    assert len(rows) == 1
    assert rows[0]["api"] == "openai-codex-responses"
    assert rows[0]["maxTokens"] == 128_000


@pytest.mark.asyncio
async def test_manifest_rejects_conflicting_gateway_credentials(async_client) -> None:
    first = await _key(async_client)
    second = await _key(async_client)
    response = await async_client.get(
        "/v1/omp/models",
        headers={
            "Authorization": f"Bearer {first}",
            "x-api-key": second,
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_manifest_catalog_failure_releases_codex_catalog_reservation(async_client, monkeypatch) -> None:
    released = []

    async def enforce(*args, **kwargs):
        return "reservation"

    async def release(reservation):
        released.append(reservation)

    async def failure(*args, **kwargs):
        raise RuntimeError("fixture catalog unavailable")

    monkeypatch.setattr(proxy_api, "_enforce_request_limits", enforce)
    monkeypatch.setattr(proxy_api, "_release_reservation_deferring_cancellation", release)
    monkeypatch.setattr(proxy_api, "_list_enabled_source_catalog_models", failure)
    with pytest.raises(RuntimeError, match="fixture catalog unavailable"):
        await async_client.get("/v1/omp/models")
    assert released == ["reservation"]


@pytest.mark.asyncio
async def test_manifest_retains_chat_when_higher_priority_source_is_responses_only(async_client) -> None:
    from app.modules.model_sources.repository import ModelSourcesRepository

    responses_id = await _source("shared-model", native=False)
    chat_id = await _source("chat-owner", native=False)
    async with get_background_session() as session:
        repository = ModelSourcesRepository(session)
        responses = await repository.get_by_id(responses_id)
        chat = await repository.get_by_id(chat_id)
        assert responses is not None and chat is not None
        responses.priority = 10
        responses.supports_chat_completions = False
        responses.supports_messages = False
        responses.supports_responses = True
        responses.models[0].context_window = 64_000
        chat.models[0].model = "shared-model"
        await session.commit()

    response = await async_client.get("/v1/omp/models")
    assert response.status_code == 200
    [model] = [row for row in response.json()["models"] if row["id"] == "shared-model"]
    assert model["api"] == "openai-completions"
    assert model["basePath"] == "/v1"
    assert model["contextWindow"] == 200_000
    response = await async_client.get("/v1/models")
    [model] = [row for row in response.json()["data"] if row["id"] == "shared-model"]
    assert set(model["api_types"]) == {"responses", "chat_completions", "messages"}
