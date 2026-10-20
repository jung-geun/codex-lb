from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.auth.dependencies import validate_required_proxy_api_key_authorization
from app.core.utils.time import utcnow
from app.db.models import ModelSource, ModelSourceModel, ModelSourceOAuthCredential, ModelSourceQuotaWindow, RequestLog
from app.db.session import SessionLocal
from app.modules.model_sources.selection import source_is_eligible

pytestmark = pytest.mark.integration


async def _claude_source(source_id: str, ratio: float = 0.5) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            ModelSource(
                id=source_id,
                name=f"Claude {source_id}",
                kind="anthropic_compatible",
                base_url="https://api.anthropic.com",
                provider="claude",
                auth_type="claude_oauth",
                billing_mode="subscription",
                is_enabled=True,
                entitlement_status="verified",
                entitlement_verified_at=now,
                extra_usage_enabled=False,
                supports_messages=True,
                supports_chat_completions=False,
                supports_responses=False,
                quota_checked_at=now,
                quota_error=None,
                oauth_credential=ModelSourceOAuthCredential(
                    source_id=source_id,
                    access_token_encrypted=b"source-only-secret",
                    expires_at=now + timedelta(hours=1),
                    account_uuid=source_id,
                    generation=1,
                ),
                models=[ModelSourceModel(model="claude-configured", is_enabled=True, subscription_eligible=True)],
                quota_windows=[
                    ModelSourceQuotaWindow(
                        window_key="weekly",
                        model=None,
                        used_ratio=ratio,
                        observed_at=now,
                        resets_at=now + timedelta(days=1),
                    )
                ],
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_management_keeps_exhausted_claude_selectable_without_widening_discovery(async_client):
    await _claude_source("exhausted", ratio=1)
    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "Claude only",
            "billingPolicy": "subscription_only",
            "assignedSourceIds": ["exhausted"],
            "allowedModels": ["claude-configured"],
            "enforcedModel": "claude-configured",
        },
    )
    assert created.status_code == 200
    key_id, secret = created.json()["id"], created.json()["key"]
    management = await async_client.get("/api/api-keys/source-access", params={"key_id": key_id})
    assert management.status_code == 200
    assert management.headers["cache-control"] == "no-store"
    entry = management.json()["models"][0]
    assert entry["sourceId"] == "exhausted"
    assert entry["model"] == "claude-configured"
    assert entry["availability"] == "quota_exhausted"
    assert entry["quotaWindows"][0]["usedRatio"] == 1
    assert "source-only-secret" not in management.text
    assert secret not in management.text
    assert "accessToken" not in management.text
    key = await validate_required_proxy_api_key_authorization(f"Bearer {secret}")
    async with SessionLocal() as session:
        from app.modules.model_sources.repository import ModelSourcesRepository

        sources = list(await ModelSourcesRepository(session).list_sources())
        assert not source_is_eligible(sources[0], key, "claude-configured", protocol="messages", group_sources=sources)
    discovery = await async_client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
    assert discovery.status_code == 200
    assert "claude-configured" not in [entry["id"] for entry in discovery.json()["data"]]
    unchanged = await async_client.patch(f"/api/api-keys/{key_id}", json={"name": "Renamed"})
    assert unchanged.json()["assignedSourceIds"] == ["exhausted"]
    assert unchanged.json()["allowedModels"] == ["claude-configured"]
    assert unchanged.json()["enforcedModel"] == "claude-configured"


@pytest.mark.asyncio
async def test_real_gateway_key_policy_intersects_source_model_and_enforced_model(async_client):
    await _claude_source("allowed")
    await _claude_source("denied")
    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "Scoped",
            "billingPolicy": "subscription_only",
            "assignedSourceIds": ["allowed"],
            "allowedModels": ["claude-configured"],
            "enforcedModel": "claude-configured",
        },
    )
    assert created.status_code == 200
    key = await validate_required_proxy_api_key_authorization(f"Bearer {created.json()['key']}")
    async with SessionLocal() as session:
        from app.modules.model_sources.repository import ModelSourcesRepository

        sources = list(await ModelSourcesRepository(session).list_sources())
        allowed = next(source for source in sources if source.id == "allowed")
        denied = next(source for source in sources if source.id == "denied")
        assert source_is_eligible(allowed, key, "claude-configured", protocol="messages", group_sources=sources)
        assert not source_is_eligible(denied, key, "claude-configured", protocol="messages", group_sources=sources)
        assert not source_is_eligible(allowed, key, "other-model", protocol="messages", group_sources=sources)
    access = await async_client.get("/api/api-keys/source-access", params={"key_id": key.id})
    assert {entry["sourceId"]: entry["availability"] for entry in access.json()["models"]} == {
        "allowed": "available",
        "denied": "restricted",
    }
    invalid = await async_client.patch(f"/api/api-keys/{key.id}", json={"enforcedModel": "other-model"})
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_key_costs_include_unpriced_and_zero_cost_subscription_accounts(async_client):
    await _claude_source("cost-source")
    created = await async_client.post("/api/api-keys/", json={"name": "usage"})
    key_id = created.json()["id"]
    async with SessionLocal() as session:
        for index, cost in enumerate([None, 0, 0.25]):
            session.add(
                RequestLog(
                    api_key_id=key_id,
                    request_id=f"source-cost-{index}",
                    requested_at=utcnow() - timedelta(minutes=1),
                    model_source_id="cost-source",
                    provider="claude",
                    billing_mode="subscription",
                    model="claude-configured",
                    status="success",
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=cost,
                )
            )
        await session.commit()
    usage = await async_client.get(f"/api/api-keys/{key_id}/usage-7d")
    assert usage.status_code == 200
    payload = usage.json()
    assert payload["totalRequests"] == 3
    assert payload["totalTokens"] == 360
    assert payload["totalCostUsd"] == 0.25
    assert len(payload["accountCosts"]) == 1
    source = payload["accountCosts"][0]
    assert source["modelSourceId"] == "cost-source"
    assert source["sourceName"] == "Claude cost-source"
    assert source["provider"] == "claude"
    assert source["billingMode"] == "subscription"
    assert source["requestCount"] == 3
    assert source["unpricedRequestCount"] == 1
    assert source["costUsd"] == 0.25
