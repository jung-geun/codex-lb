from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import RequestLog
from app.db.session import SessionLocal
from app.modules.model_sources import service as source_service

pytestmark = pytest.mark.integration


async def create_source(client, name="usage"):
    response = await client.post("/api/model-sources/", json={
        "name": name, "baseUrl": "http://localhost:9000/v1", "apiKey": "synthetic-secret",
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_usage_sums_only_retained_source_logs_and_preserves_partial_unknown_cost(async_client, monkeypatch):
    source_id = await create_source(async_client)
    other_id = await create_source(async_client, "other")
    now = datetime(2026, 10, 18, 12)
    monkeypatch.setattr(source_service, "utcnow", lambda: now)

    def log(request_id, **overrides):
        fields = dict(
            model_source_id=source_id, provider="openai", billing_mode="api_metered", model="openai/gpt-example",
            request_id=request_id, requested_at=now - timedelta(hours=1), status="success",
            input_tokens=10, output_tokens=2, cached_input_tokens=3, cache_creation_input_tokens=4,
            reasoning_tokens=1, cost_usd=0.25, api_key_id="private-api-key-id", client_ip="192.0.2.10",
            conversation_id="private-conversation", error_message="private-upstream-detail",
            useragent="private-agent", session_id="private-session",
        )
        fields.update(overrides)
        return RequestLog(**fields)

    async with SessionLocal() as session:
        session.add_all([
            log("known"),
            log("unknown", cost_usd=None, input_tokens=None, output_tokens=None, reasoning_tokens=None, status="error"),
            log("cancelled", cost_usd=0.0, status="cancelled", requested_at=now - timedelta(minutes=5)),
            log("old", requested_at=now - timedelta(hours=24, seconds=1)),
            log("future", requested_at=now + timedelta(seconds=1)),
            log("deleted", deleted_at=now),
            log("warmup", request_kind="warmup"),
            log("limit-warmup", request_kind="limit_warmup"),
            log("other-source", model_source_id=other_id),
            log("codex-owned", model_source_id=None, model_source_kind="subscription"),
        ])
        await session.commit()
    response = await async_client.get(f"/api/model-sources/{source_id}/usage")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["sourceId"] == source_id
    assert data["windowStart"].startswith("2026-10-17T12:00:00")
    assert data["windowEnd"].startswith("2026-10-18T12:00:00")
    assert data["requestCount"] == 3
    assert data["successCount"] == 1 and data["errorCount"] == 1 and data["cancelledCount"] == 1
    assert data["inputTokens"] == 20 and data["outputTokens"] == 4
    assert data["cachedInputTokens"] == 9 and data["cacheCreationInputTokens"] == 12
    assert data["reasoningTokens"] == 2
    assert data["costUsd"] is None and data["knownCostUsd"] == 0.25
    assert data["unpricedRequestCount"] == 1 and data["unknownUsageCount"] == 1
    assert data["lastRequestAt"].startswith("2026-10-18T11:55:00")
    assert not any(value in response.text for value in (
        "synthetic-secret", "private-api-key-id", "192.0.2.10", "private-conversation",
        "private-upstream-detail", "private-agent", "private-session",
    ))
    assert set(data) == {
        "sourceId", "windowStart", "windowEnd", "requestCount", "successCount", "errorCount", "cancelledCount",
        "inputTokens", "outputTokens", "cachedInputTokens", "cacheCreationInputTokens", "reasoningTokens",
        "costUsd", "knownCostUsd", "unpricedRequestCount", "unknownUsageCount", "lastRequestAt",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("costs,expected_total,expected_known,unpriced", [
    ([], 0.0, 0.0, 0),
    ([None], None, None, 1),
    ([0.0], 0.0, 0.0, 0),
    ([0.25, 0.75], 1.0, 1.0, 0),
    ([0.25, None], None, 0.25, 1),
])
async def test_usage_cost_null_zero_and_known_values_are_distinct(
    async_client, costs, expected_total, expected_known, unpriced
):
    source_id = await create_source(async_client)
    async with SessionLocal() as session:
        session.add_all([
            RequestLog(model_source_id=source_id, request_id=f"cost-{index}", model="openai/gpt-example",
                       status="success", cost_usd=cost, input_tokens=10, output_tokens=0)
            for index, cost in enumerate(costs)
        ])
        await session.commit()
    response = await async_client.get(f"/api/model-sources/{source_id}/usage")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["requestCount"] == len(costs)
    assert data["costUsd"] == expected_total
    assert data["knownCostUsd"] == expected_known
    assert data["unpricedRequestCount"] == unpriced
    assert data["unknownUsageCount"] == 0
    if not costs:
        assert data["lastRequestAt"] is None


@pytest.mark.asyncio
async def test_usage_missing_source_is_not_an_empty_success_even_with_orphan_logs(async_client):
    async with SessionLocal() as session:
        session.add(RequestLog(model_source_id="missing", request_id="orphan", model="example", status="success"))
        await session.commit()
    response = await async_client.get("/api/model-sources/missing/usage")
    assert response.status_code == 404
