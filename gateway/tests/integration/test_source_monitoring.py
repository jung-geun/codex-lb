from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import TokenEncryptor
from app.db.models import ModelSourceOAuthCredential, ModelSourceQuotaObservation
from app.db.session import SessionLocal
from app.modules.model_sources import service as source_service
from app.modules.model_sources import subscription_runtime as runtime
from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import ProviderError, ProviderSnapshot, QuotaWindow

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 18, 12, 30)


@pytest.fixture
async def monitoring_source(async_client, monkeypatch):
    @asynccontextmanager
    async def background():
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(runtime, "get_background_session", background)
    monkeypatch.setattr(runtime, "utcnow", lambda: NOW)
    monkeypatch.setattr(source_service, "utcnow", lambda: NOW)
    created = await async_client.post(
        "/api/model-sources/",
        json={
            "name": "Claude monitoring",
            "provider": "claude",
            "baseUrl": "https://api.anthropic.com",
            "authType": "claude_oauth",
            "billingMode": "subscription",
        },
    )
    assert created.status_code == 200, created.text
    source_id = created.json()["id"]
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        session.add(
            ModelSourceOAuthCredential(
                source_id=source_id,
                access_token_encrypted=encryptor.encrypt("private-access"),
                refresh_token_encrypted=encryptor.encrypt("private-refresh"),
                expires_at=NOW + timedelta(hours=1),
                account_uuid="principal",
                organization_uuid="organization",
                account_label="owner@example.test",
                subscription_type="max",
                generation=0,
            )
        )
        await session.commit()
    return source_id


def snapshot(ratio=0.25):
    return ProviderSnapshot(
        billing_mode="subscription",
        entitlement_status="verified",
        extra_usage_enabled=False,
        account_uuid="principal",
        organization_uuid="organization",
        account_label="owner@example.test",
        subscription_type="max",
        windows=[
            QuotaWindow("five_hour", used_ratio=ratio, resets_at=NOW + timedelta(hours=2)),
            QuotaWindow("seven_day", used_ratio=1.0, resets_at=NOW + timedelta(days=2)),
        ],
    )


@pytest.mark.asyncio
async def test_monitoring_exposes_plan_and_presence_not_credentials(async_client, monitoring_source):
    result = await async_client.get("/api/model-sources/")
    assert result.headers.get("cache-control") == "no-store"
    source = next(item for item in result.json()["sources"] if item["id"] == monitoring_source)
    assert source["subscriptionType"] == "max"
    assert source["accountUuid"] == "principal" and source["organizationUuid"] == "organization"
    assert source["hasAccessToken"] is True and source["hasRefreshToken"] is True
    assert source["accountLabel"] == "owner@example.test"
    assert "private-access" not in result.text and "private-refresh" not in result.text


@pytest.mark.asyncio
async def test_quota_history_returns_only_actual_newest_hour_samples_in_window(async_client, monitoring_source):
    async with SessionLocal() as session:
        session.add_all(
            [
                ModelSourceQuotaObservation(
                    source_id=monitoring_source,
                    window_key="five_hour",
                    used_ratio=ratio,
                    observed_at=at,
                    resets_at=NOW + timedelta(hours=1),
                )
                for at, ratio in [
                    (NOW - timedelta(days=8), 0.8),
                    (NOW - timedelta(hours=2, minutes=20), 0.1),
                    (NOW - timedelta(hours=2, minutes=10), 0.4),
                    (NOW - timedelta(minutes=30), 1.0),
                    (NOW + timedelta(seconds=1), 0.0),
                ]
            ]
        )
        await session.commit()
    response = await async_client.get(f"/api/model-sources/{monitoring_source}/quota-history")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["sourceId"] == monitoring_source
    assert data["windowStart"] == "2026-10-11T12:30:00Z"
    assert data["windowEnd"] == "2026-10-18T12:30:00Z"
    assert [(row["observedAt"], row["usedRatio"]) for row in data["windows"]] == [
        ("2026-10-18T10:20:00Z", 0.4),
        ("2026-10-18T12:00:00Z", 1.0),
    ]
    assert (await async_client.get("/api/model-sources/missing/quota-history")).status_code == 404
    assert (await async_client.get(f"/api/model-sources/{monitoring_source}/quota-history?days=31")).status_code == 422
    assert (await async_client.get(f"/api/model-sources/{monitoring_source}/quota-history?days=0")).status_code == 422


@pytest.mark.asyncio
async def test_snapshot_retains_real_quota_history_and_failed_refresh_adds_nothing(
    async_client,
    monitoring_source,
    monkeypatch,
):
    source = await runtime.load_source(monitoring_source)
    await runtime._persist_snapshot(source, snapshot())
    later = NOW + timedelta(hours=1)
    monkeypatch.setattr(runtime, "utcnow", lambda: later)
    monkeypatch.setattr(source_service, "utcnow", lambda: later)
    await runtime._persist_snapshot(await runtime.load_source(monitoring_source), snapshot(0.5))

    async def failed_inspect(token):
        raise ProviderError("Provider metadata temporarily unavailable")

    monkeypatch.setattr(runtime.claude, "inspect", failed_inspect)
    # Keep the test credential valid so this exercises quota failure, not OAuth rotation.
    async with SessionLocal() as session:
        row = await session.get(ModelSourceOAuthCredential, monitoring_source)
        row.expires_at = NOW + timedelta(days=1)
        await session.commit()
    result = await runtime.refresh_source_quota(monitoring_source)
    assert result.quota_error is not None and result.health_status == "error"
    history = (await async_client.get(f"/api/model-sources/{monitoring_source}/quota-history")).json()["windows"]
    assert [(row["observedAt"], row["usedRatio"]) for row in history if row["windowKey"] == "five_hour"] == [
        ("2026-10-18T12:30:00Z", 0.25),
        ("2026-10-18T13:30:00Z", 0.5),
    ]
    assert result.quota_windows[0].used_ratio == 0.5


@pytest.mark.asyncio
async def test_reauthorization_clears_old_principal_history_and_stale_writer_cannot_restore_it(
    async_client,
    monitoring_source,
):
    original = await runtime.load_source(monitoring_source)
    await runtime._persist_snapshot(original, snapshot())
    await runtime.install_credential(
        monitoring_source,
        claude.TokenPair(access_token="new-access", refresh_token="new-refresh", expires_in=3600),
        expected_generation=0,
        expected_configuration=runtime.configuration_fingerprint(original),
    )
    with pytest.raises(ProviderError, match="changed during quota refresh"):
        await runtime._persist_snapshot(original, snapshot())
    response = await async_client.get(f"/api/model-sources/{monitoring_source}/quota-history")
    assert response.status_code == 200 and response.json()["windows"] == []
    source = await runtime.load_source(monitoring_source)
    assert source.quota_windows == [] and source.oauth_credential.account_uuid is None


@pytest.mark.asyncio
async def test_observation_retention_and_source_identity_change_do_not_mix_history(
    async_client,
    monitoring_source,
):
    async with SessionLocal() as session:
        session.add(
            ModelSourceQuotaObservation(
                source_id=monitoring_source,
                window_key="five_hour",
                used_ratio=0.99,
                observed_at=NOW - timedelta(days=31),
            )
        )
        await session.commit()
    await runtime._persist_snapshot(await runtime.load_source(monitoring_source), snapshot())
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(ModelSourceQuotaObservation))).all())
        assert len(rows) == 2 and all(row.observed_at == NOW for row in rows)
    # A source credential replacement must not show another principal's observations.
    updated = await async_client.patch(
        f"/api/model-sources/{monitoring_source}",
        json={
            "provider": "anthropic",
            "apiKey": "replacement-api-key",
        },
    )
    assert updated.status_code == 200, updated.text
    assert (await async_client.get(f"/api/model-sources/{monitoring_source}/quota-history")).json()["windows"] == []
