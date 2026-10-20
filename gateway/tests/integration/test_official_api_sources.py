from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.core.auth.dependencies import validate_required_proxy_api_key_authorization
from app.core.crypto import TokenEncryptor
from app.db.models import ModelSource
from app.db.session import SessionLocal
from app.modules.model_sources import subscription_runtime as runtime
from app.modules.model_sources.providers import official_apis
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.selection import select_model_source

pytestmark = pytest.mark.integration


@pytest.fixture
async def official_runtime(db_setup, monkeypatch):
    @asynccontextmanager
    async def background():
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(runtime, "get_background_session", background)


async def create_official(client, provider, **extra):
    payload = {
        "name": provider,
        "provider": provider,
        "baseUrl": "https://api.openai.com/v1" if provider == "openai" else "https://api.anthropic.com",
        "apiKey": "synthetic-api-key",
    }
    payload.update(extra)
    return await client.post("/api/model-sources/", json=payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,protocol,native", [
    ("openai", "chat_completions", "gpt-5.1"),
    ("anthropic", "messages", "claude-example"),
])
async def test_official_catalog_is_authenticated_metered_namespaced_and_source_scoped(
    async_client, official_runtime, monkeypatch, provider, protocol, native
):
    observed = []

    async def request(method, url, *, headers):
        observed.append((method, url, headers))
        return {"data": [{"id": native}], "has_more": False}

    monkeypatch.setattr(official_apis, "request_json", request)
    created = await create_official(async_client, provider)
    assert created.status_code == 200, created.text
    source_id = created.json()["id"]
    assert created.json()["billingMode"] == "api_metered"
    checked = await async_client.post(f"/api/model-sources/{source_id}/quota")
    assert checked.status_code == 200, checked.text
    source = checked.json()
    assert source["entitlementStatus"] == "unverified"
    assert source["entitlementVerifiedAt"] is None
    assert source["healthStatus"] == "healthy"
    assert source["quotaCheckedAt"] is not None
    assert source["quotaError"] is None and source["quotaWindows"] == []
    alias = provider + "/" + native
    assert source["models"][0]["model"] == alias
    assert source["models"][0]["upstreamModel"] == native
    assert source["models"][0]["contextWindow"] is None
    assert source["models"][0]["inputPer1M"] is None
    assert source["models"][0]["subscriptionEligible"] is False
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        assert row is not None and row.oauth_credential is None
        assert row.api_key_encrypted != b"synthetic-api-key"
        assert TokenEncryptor().decrypt(row.api_key_encrypted) == "synthetic-api-key"
    selected = await select_model_source(alias, None, protocol=protocol)
    assert selected is not None and selected.id == source_id
    subscription_key = await async_client.post("/api/api-keys/", json={
        "name": "subscription", "billingPolicy": "subscription_only", "assignedSourceIds": [source_id],
    })
    subscription = await validate_required_proxy_api_key_authorization("Bearer " + subscription_key.json()["key"])
    assert await select_model_source(alias, subscription, protocol=protocol) is None
    unrelated = await async_client.post("/api/model-sources/", json={
        "name": "unrelated", "baseUrl": "http://localhost:9000/v1", "models": [{"model": "other"}],
    })
    assert unrelated.status_code == 200
    other = await async_client.post("/api/api-keys/", json={
        "name": "other-scope", "assignedSourceIds": [unrelated.json()["id"]],
    })
    scoped = await validate_required_proxy_api_key_authorization("Bearer " + other.json()["key"])
    assert await select_model_source(alias, scoped, protocol=protocol) is None
    # Unscoped native IDs can now resolve to the same exact API catalog model.
    # Registry-native Codex IDs still need caller-proven subscription unavailability.
    native_selection = await select_model_source(native, None, protocol=protocol)
    assert native_selection is not None and native_selection.id == source_id
    assert len(observed) == 1 and observed[0][0] == "GET"
    assert "synthetic-api-key" not in checked.text


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-example", "anthropic/claude-example"])
@pytest.mark.parametrize("route", ["responses", "chat/completions"])
async def test_messages_only_provider_cannot_fall_through_to_codex(
    async_client, official_runtime, monkeypatch, model, route,
):
    async def catalog(*args, **kwargs):
        return {"data": [{"id": "claude-example"}], "has_more": False}

    monkeypatch.setattr(official_apis, "request_json", catalog)
    created = await create_official(async_client, "anthropic")
    source_id = created.json()["id"]
    checked = await async_client.post(f"/api/model-sources/{source_id}/quota")
    assert checked.status_code == 200, checked.text
    assert [(entry["model"], entry["upstreamModel"]) for entry in checked.json()["models"]] == [
        ("anthropic/claude-example", "claude-example"),
    ]
    assert checked.json()["supportsResponses"] is False
    enabled = await async_client.put("/api/settings", json={"apiKeyAuthEnabled": True})
    assert enabled.status_code == 200, enabled.text
    created_key = await async_client.post("/api/api-keys/", json={"name": "unscoped provider boundary"})
    payload = {"model": model, "stream": False}
    if route == "responses":
        payload.update(instructions="", input="hello")
    else:
        payload["messages"] = [{"role": "user", "content": "hello"}]
    denied = await async_client.post(
        f"/v1/{route}", json=payload,
        headers={"Authorization": "Bearer " + created_key.json()["key"]},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "model_source_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("override", [
    {"baseUrl": "http://api.openai.com/v1"},
    {"billingMode": "subscription"},
    {"authType": "none"},
    {"apiKey": None},
])
async def test_official_presets_reject_wrong_origin_auth_or_subscription(async_client, provider, override):
    response = await create_official(async_client, provider, **override)
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_official_refresh_persists_safe_failure_and_recovers_without_subscription(
    async_client, official_runtime, monkeypatch
):
    created = await create_official(async_client, "openai")
    source_id = created.json()["id"]

    async def rejected(*args, **kwargs):
        raise ProviderError("Provider metadata request rejected (HTTP 401)", permanent=True, status=401)

    monkeypatch.setattr(official_apis, "request_json", rejected)
    failed = await async_client.post(f"/api/model-sources/{source_id}/quota")
    assert failed.status_code == 200
    assert failed.json()["healthStatus"] == "error"
    assert failed.json()["entitlementStatus"] == "reauth_required"
    assert failed.json()["quotaError"] == "Provider metadata request rejected (HTTP 401)"
    assert failed.json()["quotaCheckedAt"] is not None

    async def catalog(*args, **kwargs):
        return {"data": [{"id": "gpt-example"}]}

    monkeypatch.setattr(official_apis, "request_json", catalog)
    recovered = await async_client.post(f"/api/model-sources/{source_id}/quota")
    assert recovered.json()["healthStatus"] == "healthy"
    assert recovered.json()["quotaError"] is None
    assert recovered.json()["entitlementStatus"] == "unverified"
    assert recovered.json()["entitlementVerifiedAt"] is None


@pytest.mark.asyncio
async def test_refresh_preserves_editable_api_limits_and_alias_identity(async_client, official_runtime, monkeypatch):
    async def catalog(*args, **kwargs):
        return {"data": [{"id": "gpt-example"}]}

    monkeypatch.setattr(official_apis, "request_json", catalog)
    created = await create_official(async_client, "openai")
    source_id = created.json()["id"]
    await async_client.post(f"/api/model-sources/{source_id}/quota")
    updated = await async_client.patch(f"/api/model-sources/{source_id}", json={"models": [{
        "model": "openai/gpt-example", "upstreamModel": "gpt-example", "contextWindow": 17000,
        "maxOutputTokens": 2000, "supportsTools": True,
        "rawMetadataJson": '{"supports_reasoning":true,"supported_reasoning_levels":["high"]}',
    }]})
    assert updated.status_code == 200, updated.text
    refreshed = await async_client.post(f"/api/model-sources/{source_id}/quota")
    model, = refreshed.json()["models"]
    assert model["contextWindow"] == 17000 and model["maxOutputTokens"] == 2000
    assert model["supportsTools"] is True
    assert model["model"] == "openai/gpt-example" and model["upstreamModel"] == "gpt-example"
    async with SessionLocal() as session:
        sources = list((await session.scalars(select(ModelSource))).all())
        assert len(sources) == 1




@pytest.mark.asyncio
@pytest.mark.parametrize("supported_type,expected_mode", [
    ("adaptive", "anthropic-adaptive"),
    ("enabled", "anthropic-budget-effort"),
])
async def test_namespaced_claude_api_models_keep_provider_declared_thinking_modes(
    async_client, official_runtime, monkeypatch, supported_type, expected_mode
):
    async def catalog(*args, **kwargs):
        return {"data": [{
            "id": "new-provider-model", "max_input_tokens": 200000, "max_tokens": 8192,
            "capabilities": {"thinking": {"supported": True, "types": {supported_type: {"supported": True}}},
                             "effort": {"high": {"supported": True}, "max": {"supported": True}}},
        }], "has_more": False}

    monkeypatch.setattr(official_apis, "request_json", catalog)
    created = await create_official(async_client, "anthropic")
    assert created.status_code == 200
    refreshed = await async_client.post(f"/api/model-sources/{created.json()['id']}/quota")
    assert refreshed.status_code == 200
    manifest = await async_client.get("/v1/omp/models")
    assert manifest.status_code == 200
    model = next(row for row in manifest.json()["models"] if row["id"] == "anthropic/new-provider-model")
    assert model["thinking"]["mode"] == expected_mode
    assert model["thinking"]["efforts"] == ["high", "max"]
