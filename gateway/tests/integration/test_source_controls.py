from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.audit.service import AuditService
from app.core.auth.dependencies import require_dashboard_write_access
from app.core.exceptions import DashboardPermissionError
from app.db.models import ModelSource, ModelSourceLimitWarmup, ModelSourceQuotaObservation, ModelSourceQuotaWindow
from app.db.session import SessionLocal
from app.modules.model_sources import service as source_service
from app.modules.model_sources import subscription_warmup
from app.modules.model_sources.provider_schemas import presets
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.schemas import ModelSourceResponse

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 19, 12)


@pytest.fixture
async def control_source(async_client):
    response = await async_client.post(
        "/api/model-sources/",
        json={"name": "Claude controls", "provider": "claude", "baseUrl": "https://api.anthropic.com"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_controls_default_off_and_policy_and_pause_persist(async_client, control_source, monkeypatch):
    source_id = control_source["id"]
    assert control_source["routingPolicy"] == "normal"
    assert control_source["limitWarmupEnabled"] is False
    assert control_source["limitWarmup"] is None
    assert control_source["warmupSupported"] is True
    actions = []
    monkeypatch.setattr(AuditService, "log_async", lambda action, **kwargs: actions.append((action, kwargs)))
    for policy in ("burn_first", "preserve", "normal"):
        response = await async_client.patch(
            f"/api/model-sources/{source_id}",
            json={"routingPolicy": policy, "limitWarmupEnabled": True, "isEnabled": False},
        )
        assert response.status_code == 200, response.text
        assert response.json()["routingPolicy"] == policy
        assert response.json()["limitWarmupEnabled"] is True
        assert response.json()["isEnabled"] is False
        assert response.json()["warmupUnavailableReason"] == "disabled"
        async with SessionLocal() as session:
            stored = await session.get(ModelSource, source_id)
            assert stored.routing_policy == policy
            assert stored.limit_warmup_enabled is True
            assert stored.is_enabled is False
    assert [action for action, _ in actions] == ["model_source_updated"] * 3
    assert all(details["target"].id == source_id for _, details in actions)
    resumed = await async_client.patch(f"/api/model-sources/{source_id}", json={"isEnabled": True})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["isEnabled"] is True
    assert resumed.json()["warmupUnavailableReason"] != "disabled"
    listed = await async_client.get("/api/model-sources/")
    source = next(item for item in listed.json()["sources"] if item["id"] == source_id)
    assert source["routingPolicy"] == "normal" and source["limitWarmupEnabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,status",
    [
        ({"routingPolicy": "fastest"}, 422),
        ({"routingPolicy": None}, 400),
        ({"limitWarmupEnabled": None}, 400),
    ],
)
async def test_invalid_controls_are_rejected_without_persistence(async_client, control_source, payload, status):
    source_id = control_source["id"]
    response = await async_client.patch(f"/api/model-sources/{source_id}", json=payload)
    assert response.status_code == status, response.text
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        assert row.routing_policy == "normal"
        assert row.limit_warmup_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,base_url",
    [
        ("anthropic", "https://api.anthropic.com"),
        ("openai", "https://api.openai.com/v1"),
        ("custom", "https://metered.example/v1"),
    ],
)
async def test_metered_sources_do_not_expose_fake_warmup_controls(async_client, provider, base_url):
    created = await async_client.post(
        "/api/model-sources/",
        json={"name": "Metered", "provider": provider, "baseUrl": base_url, "apiKey": "synthetic-key"},
    )
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["warmupSupported"] is False
    assert data["limitWarmupEnabled"] is False
    assert data["limitWarmup"] is None
    assert data["warmupUnavailableReason"] is not None
    response = await async_client.patch(
        f"/api/model-sources/{data['id']}",
        json={"routingPolicy": "preserve", "limitWarmupEnabled": True},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_model_source_payload"
    async with SessionLocal() as session:
        stored = await session.get(ModelSource, data["id"])
        assert stored.routing_policy == "normal" and stored.limit_warmup_enabled is False
    manual = await async_client.post(f"/api/model-sources/{data['id']}/warmup")
    assert manual.status_code == 400, manual.text
    assert manual.json()["error"]["code"] == "provider_connection_error"
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceLimitWarmup))).all() == []


@pytest.mark.asyncio
async def test_last_warmup_uses_newest_durable_attempt_and_iso_dates(async_client, control_source):
    source_id = control_source["id"]
    async with SessionLocal() as session:
        session.add_all(
            [
                ModelSourceLimitWarmup(
                    id="older-control-attempt",
                    source_id=source_id,
                    principal="principal:organization",
                    window_key="five_hour",
                    window_identity="prior",
                    status="success",
                    model="claude-sonnet-4-6",
                    reset_at=NOW,
                    attempted_at=NOW - timedelta(hours=5),
                    completed_at=NOW - timedelta(hours=5),
                ),
                ModelSourceLimitWarmup(
                    id="newest-control-attempt",
                    source_id=source_id,
                    principal="principal:organization",
                    window_key="five_hour",
                    window_identity="current",
                    status="unknown",
                    model="claude-sonnet-4-6",
                    reset_at=NOW + timedelta(hours=5),
                    attempted_at=NOW,
                    error_message="Completion not confirmed",
                ),
            ]
        )
        await session.commit()
    response = await async_client.get("/api/model-sources/")
    source = next(item for item in response.json()["sources"] if item["id"] == source_id)
    assert source["limitWarmup"] == {
        "window": "five_hour",
        "resetAt": "2026-10-19T17:00:00Z",
        "status": "unknown",
        "model": "claude-sonnet-4-6",
        "attemptedAt": "2026-10-19T12:00:00Z",
        "completedAt": None,
        "errorMessage": "Completion not confirmed",
    }


@pytest.mark.asyncio
async def test_deleting_source_preserves_principal_window_claim(async_client, control_source):
    source_id = control_source["id"]
    async with SessionLocal() as session:
        session.add(
            ModelSourceLimitWarmup(
                id="permanent-control-claim",
                source_id=source_id,
                principal="principal:organization",
                window_key="five_hour",
                window_identity="current",
                status="success",
                model="claude-sonnet-4-6",
                attempted_at=NOW,
                completed_at=NOW,
            )
        )
        await session.commit()
    response = await async_client.delete(f"/api/model-sources/{source_id}")
    assert response.status_code == 204, response.text
    async with SessionLocal() as session:
        claim = await session.get(ModelSourceLimitWarmup, "permanent-control-claim")
        assert claim is not None
        assert claim.source_id is None
        assert claim.principal == "principal:organization" and claim.window_identity == "current"


@pytest.mark.asyncio
async def test_manual_warmup_delegates_to_runtime_and_audits(async_client, control_source, monkeypatch):
    calls, actions = [], []

    async def warm(source_id, *, manual=True):
        calls.append((source_id, manual))
        return ModelSourceResponse.model_validate(control_source)

    monkeypatch.setattr(subscription_warmup, "warm_source", warm)
    monkeypatch.setattr(AuditService, "log_async", lambda action, **kwargs: actions.append((action, kwargs)))
    response = await async_client.post(f"/api/model-sources/{control_source['id']}/warmup")
    assert response.status_code == 200, response.text
    assert response.json() == control_source
    assert calls == [(control_source["id"], True)]
    assert [action for action, _ in actions] == ["model_source_warmup_requested"]
    assert actions[0][1]["details"] == {"source_id": control_source["id"]}


@pytest.mark.asyncio
async def test_warmup_errors_use_standard_provider_envelope(async_client, control_source, monkeypatch):
    async def rejected(source_id):
        raise ProviderError("Source is paused")

    monkeypatch.setattr(subscription_warmup, "warm_source", rejected)
    response = await async_client.post(f"/api/model-sources/{control_source['id']}/warmup")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "provider_connection_error"
    assert response.json()["error"]["message"] == "Source is paused"


@pytest.mark.asyncio
async def test_control_mutations_require_dashboard_write_permission(app_instance, async_client, monkeypatch):
    calls = []

    async def denied():
        raise DashboardPermissionError(
            "Read-only dashboard access cannot modify dashboard state", code="read_only_access"
        )

    async def unexpected_warmup(source_id):
        calls.append(source_id)
        raise AssertionError("Read-only caller reached warmup runtime")

    monkeypatch.setattr(subscription_warmup, "warm_source", unexpected_warmup)
    app_instance.dependency_overrides[require_dashboard_write_access] = denied
    try:
        responses = [
            await async_client.patch("/api/model-sources/missing", json={"routingPolicy": "preserve"}),
            await async_client.patch("/api/model-sources/missing", json={"limitWarmupEnabled": True}),
            await async_client.post("/api/model-sources/missing/warmup"),
        ]
    finally:
        app_instance.dependency_overrides.pop(require_dashboard_write_access, None)
    assert all(response.status_code == 403 for response in responses)
    assert all(response.json()["error"]["code"] == "read_only_access" for response in responses)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("age,status", [(90, "dispatching"), (91, "unknown")])
async def test_expired_dispatch_projects_unknown_even_when_readiness_blocked(
    async_client,
    control_source,
    monkeypatch,
    paused,
    age,
    status,
):
    monkeypatch.setattr(source_service, "utcnow", lambda: NOW)
    source_id = control_source["id"]
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.is_enabled = not paused
        source.limit_warmup_enabled = True
        source.quota_windows.append(
            ModelSourceQuotaWindow(
                window_key="seven_day",
                used_ratio=1.0,
                observed_at=NOW,
                resets_at=NOW + timedelta(days=1),
            )
        )
        session.add(
            ModelSourceLimitWarmup(
                id="expired-control-claim",
                source_id=source_id,
                principal="principal:organization",
                window_key="five_hour",
                window_identity="current",
                status="dispatching",
                attempted_at=NOW - timedelta(seconds=age),
                model="claude-sonnet-4-6",
            )
        )
        await session.commit()
    response = await async_client.get("/api/model-sources/")
    assert response.status_code == 200, response.text
    source = next(item for item in response.json()["sources"] if item["id"] == source_id)
    assert source["limitWarmup"]["status"] == status
    assert source["limitWarmup"]["errorMessage"] == ("acceptance_unknown" if age > 90 else None)
    async with SessionLocal() as session:
        claim = await session.get(ModelSourceLimitWarmup, "expired-control-claim")
        assert claim.status == "dispatching" and claim.error_message is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["zai", "kimi"])
@pytest.mark.parametrize(
    "control",
    [
        {"routingPolicy": "preserve"},
        {"isEnabled": False},
        {"limitWarmupEnabled": False},
    ],
)
async def test_control_patch_preserves_native_verified_billing_and_quota(async_client, provider, control):
    preset = next(preset for preset in presets() if preset.provider == provider)
    created = await async_client.post(
        "/api/model-sources/",
        json={
            "name": "Verified native coding source",
            "provider": provider,
            "baseUrl": preset.base_url,
            "apiKey": "synthetic-native-key",
            "keyType": "compatible",
            "models": [{"model": "native-entitled-model"}],
        },
    )
    assert created.status_code == 200, created.text
    source_id = created.json()["id"]
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        row.billing_mode = "subscription"  # Native observation refines the preset's coding_plan label.
        row.entitlement_status, row.entitlement_verified_at = "verified", NOW
        row.entitlement_detail, row.detected_key_type = "Native evidence retained", "team"
        row.extra_usage_enabled, row.quota_checked_at = False, NOW
        row.quota_group = "native:verified:principal"
        row.models[0].subscription_eligible = True
        row.quota_windows = [
            ModelSourceQuotaWindow(
                window_key="native-weekly",
                used_ratio=0.4,
                resets_at=NOW + timedelta(days=2),
                observed_at=NOW,
            )
        ]
        credential_before = row.api_key_encrypted
        session.add(ModelSourceQuotaObservation(
            source_id=source_id, window_key="native-weekly", used_ratio=0.4,
            resets_at=NOW + timedelta(days=2), observed_at=NOW,
        ))
        await session.commit()
    updated = await async_client.patch(f"/api/model-sources/{source_id}", json=control)
    assert updated.status_code == 200, updated.text
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        assert row.billing_mode == "subscription"
        assert row.auth_type == "api_key" and row.api_key_encrypted == credential_before
        assert row.key_type == "compatible" and row.detected_key_type == "team"
        assert row.entitlement_status == "verified" and row.entitlement_verified_at == NOW
        assert row.entitlement_detail == "Native evidence retained" and row.extra_usage_enabled is False
        assert row.quota_group == "native:verified:principal" and row.quota_checked_at == NOW
        assert row.models[0].subscription_eligible is True
        assert [(window.window_key, window.used_ratio, window.observed_at) for window in row.quota_windows] == [
            ("native-weekly", 0.4, NOW),
        ]
        history = list((await session.scalars(select(ModelSourceQuotaObservation).where(
            ModelSourceQuotaObservation.source_id == source_id,
        ))).all())
        assert [(sample.window_key, sample.used_ratio, sample.observed_at) for sample in history] == [
            ("native-weekly", 0.4, NOW),
        ]
    same_provider = await async_client.patch(f"/api/model-sources/{source_id}", json={"provider": provider})
    assert same_provider.status_code == 200, same_provider.text
    assert same_provider.json()["billingMode"] == "subscription"
    assert same_provider.json()["entitlementStatus"] == "verified"
    assert same_provider.json()["quotaGroup"] == "native:verified:principal"
