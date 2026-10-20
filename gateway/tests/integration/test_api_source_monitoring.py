from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

from app.db.models import ModelSource, RequestLog
from app.db.session import SessionLocal
from app.modules.model_sources import api_usage, service
from app.modules.model_sources import subscription_runtime as runtime
from app.modules.model_sources.providers import deepseek

pytestmark = pytest.mark.integration
NOW = datetime(2030, 1, 2, 12)


@pytest.fixture
async def background_sessions(async_client, monkeypatch):
    @asynccontextmanager
    async def background():
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(api_usage, "get_background_session", background)
    monkeypatch.setattr(runtime, "get_background_session", background)
    monkeypatch.setattr(api_usage, "utcnow", lambda: NOW)
    monkeypatch.setattr(runtime, "utcnow", lambda: NOW)
    monkeypatch.setattr(service, "utcnow", lambda: NOW)


async def create_source(client, provider="openai"):
    root = {"openai": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com"}[provider]
    response = await client.post("/api/model-sources/", json={
        "name": "API account", "provider": provider, "baseUrl": root, "apiKey": "private-source-key",
        "models": [{"model": "openai/native-model", "upstreamModel": "native-model"}],
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def load(source_id):
    async with SessionLocal() as session:
        return await session.get(ModelSource, source_id)


@pytest.mark.asyncio
async def test_persisted_rate_observations_clear_on_key_cutover_and_reject_late_writer(
    async_client, background_sessions,
):
    source_id = await create_source(async_client)
    selected = await load(source_id)
    await api_usage.record_api_rate_headers(selected, "native-model", {
        "x-ratelimit-limit-tokens": "10000", "x-ratelimit-remaining-tokens": "6000",
        "x-ratelimit-reset-tokens": "30s", "authorization": "private-upstream-header",
    })
    response = await async_client.get(f"/api/model-sources/{source_id}/api-usage")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["balanceStatus"] == "unavailable" and data["balances"] == []
    assert data["balanceIsAvailable"] is None
    rate, = data["rateLimits"]
    assert rate["tokensRemaining"] == 6000 and rate["tokenLimit"] == 10000
    assert rate["tokensResetAt"] == "2030-01-02T12:00:30Z"
    assert not any(value in response.text for value in ("private-source-key", "private-upstream-header", "Bearer"))
    updated = await async_client.patch(f"/api/model-sources/{source_id}", json={"apiKey": "replacement-key"})
    assert updated.status_code == 200
    await api_usage.record_api_rate_headers(selected, "native-model", {"x-ratelimit-remaining-tokens": "1"})
    clean = (await async_client.get(f"/api/model-sources/{source_id}/api-usage")).json()
    assert clean["rateLimits"] == [] and clean["observedAt"] is None
    assert (await async_client.get("/api/model-sources/missing/api-usage")).status_code == 404


@pytest.mark.asyncio
async def test_native_balance_refresh_retains_precision_and_error_without_invented_token_quota(
    async_client, background_sessions, monkeypatch,
):
    source_id = await create_source(async_client, "deepseek")
    snapshot = deepseek.parse_snapshot({"is_available": False, "balance_infos": [{
        "currency": "CNY", "total_balance": "0.000000001", "granted_balance": "0", "topped_up_balance": "0.000000001",
    }]})
    await runtime._persist_snapshot(await load(source_id), snapshot)
    data = (await async_client.get(f"/api/model-sources/{source_id}/api-usage")).json()
    assert data["balanceStatus"] == "available" and data["balanceIsAvailable"] is False
    assert data["balances"][0]["currency"] == "CNY" and data["balances"][0]["totalBalance"] == "0.000000001"
    assert data["rateLimits"] == [] and data["balanceObservedAt"] == "2030-01-02T12:00:00Z"
    # A failed refresh preserves the last native observation, with a visible error.
    from app.modules.model_sources.providers.base import ProviderError

    async def rejected(key):
        raise ProviderError("Provider metadata temporarily unavailable")

    monkeypatch.setattr(runtime.deepseek, "inspect", rejected)
    await runtime.refresh_source_quota(source_id)
    failed = (await async_client.get(f"/api/model-sources/{source_id}/api-usage")).json()
    assert failed["balances"] == data["balances"]
    assert failed["balanceObservedAt"] == data["balanceObservedAt"]
    assert failed["balanceError"] == "Provider metadata temporarily unavailable"
    assert not api_usage.api_rate_limit_available(await load(source_id), "native-model", now=NOW)


@pytest.mark.asyncio
async def test_fallback_setting_survives_unrelated_patch_and_usage_periods_keep_missing_usage(
    async_client, background_sessions,
):
    source_id = await create_source(async_client)
    changed = await async_client.patch(f"/api/model-sources/{source_id}", json={"apiFallbackOnly": False})
    assert changed.status_code == 200 and changed.json()["apiFallbackOnly"] is False
    renamed = await async_client.patch(f"/api/model-sources/{source_id}", json={"name": "renamed"})
    assert renamed.json()["apiFallbackOnly"] is False
    invalid = await async_client.patch(f"/api/model-sources/{source_id}", json={"apiFallbackOnly": None})
    assert invalid.status_code == 400
    async with SessionLocal() as session:
        for index, age in enumerate((0, 3, 20, 31)):
            session.add(RequestLog(model_source_id=source_id, model="native-model", request_id=f"period-{index}",
                requested_at=NOW - timedelta(days=age), status="success", input_tokens=None if age == 3 else 100,
                output_tokens=None if age == 3 else 20, cost_usd=None if age == 3 else 0))
        await session.commit()
    day = (await async_client.get(f"/api/model-sources/{source_id}/usage?days=1")).json()
    week = (await async_client.get(f"/api/model-sources/{source_id}/usage?days=7")).json()
    month = (await async_client.get(f"/api/model-sources/{source_id}/usage?days=30")).json()
    assert (day["requestCount"], week["requestCount"], month["requestCount"]) == (1, 2, 3)
    assert (month["inputTokens"], month["outputTokens"]) == (200, 40)
    assert month["unknownUsageCount"] == 1 and month["unpricedRequestCount"] == 1 and month["costUsd"] is None
    assert (await async_client.get(f"/api/model-sources/{source_id}/usage?days=31")).status_code == 422


@pytest.mark.asyncio
async def test_reported_zero_and_missing_token_fields_are_distinct(async_client, background_sessions):
    source_id = await create_source(async_client)
    async with SessionLocal() as session:
        session.add(RequestLog(
            model_source_id=source_id, model="native-model", request_id="partial-zero",
            requested_at=NOW, status="error", input_tokens=0, output_tokens=None,
            cached_input_tokens=0, cache_creation_input_tokens=None, reasoning_tokens=None,
        ))
        await session.commit()
    data = (await async_client.get(f"/api/model-sources/{source_id}/usage")).json()
    assert data["inputTokens"] == 0 and data["cachedInputTokens"] == 0
    assert data["outputTokens"] is None and data["cacheCreationInputTokens"] is None
    assert data["reasoningTokens"] is None and data["unknownUsageCount"] == 1
    assert data["costUsd"] is None
