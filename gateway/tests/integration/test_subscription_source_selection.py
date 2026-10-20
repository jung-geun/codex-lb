from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.auth.dependencies import validate_required_proxy_api_key_authorization
from app.db.models import ModelSource, ModelSourceOAuthCredential, ModelSourceQuotaWindow
from app.db.session import SessionLocal
from app.modules.model_sources.selection import (
    responses_model_is_source_owned,
    select_model_source,
)

pytestmark = pytest.mark.integration


async def create_source(client, name, *, priority=0):
    response = await client.post(
        "/api/model-sources/",
        json={
            "name": name,
            "baseUrl": "http://localhost:9000/v1",
            "priority": priority,
            "supportsResponses": True,
            "models": [{"model": "source-only-model"}],
        },
    )
    assert response.status_code in {200, 201}
    return response.json()["id"]


@pytest.mark.asyncio
async def test_exact_scope_priority_exclusion_and_bounded_affinity(async_client):
    first = await create_source(async_client, "first", priority=10)
    second = await create_source(async_client, "second", priority=0)
    created = await async_client.post("/api/api-keys/", json={"name": "routing"})
    assert created.status_code == 200
    api_key = await validate_required_proxy_api_key_authorization(f"Bearer {created.json()['key']}")
    chosen = await select_model_source("source-only-model", api_key, protocol="responses", affinity_key="conversation")
    assert chosen is not None and chosen.id == first
    async with SessionLocal() as session:
        row = await session.get(ModelSource, second)
        assert row is not None
        row.priority = 20
        await session.commit()
    pinned = await select_model_source("source-only-model", api_key, protocol="responses", affinity_key="conversation")
    assert pinned is not None and pinned.id == first
    unpinned = await select_model_source("source-only-model", api_key, protocol="responses")
    assert unpinned is not None and unpinned.id == second
    excluded = await select_model_source(
        "source-only-model", api_key, protocol="responses", affinity_key="conversation", exclude_source_ids={first}
    )
    assert excluded is not None and excluded.id == second
    update = await async_client.patch(f"/api/api-keys/{api_key.id}", json={"assignedSourceIds": [first]})
    assert update.status_code == 200
    scoped = await validate_required_proxy_api_key_authorization(f"Bearer {created.json()['key']}")
    chosen = await select_model_source("source-only-model", scoped, protocol="responses")
    assert chosen is not None and chosen.id == first
    await async_client.patch(f"/api/model-sources/{first}", json={"isEnabled": False})
    assert await select_model_source("source-only-model", scoped, protocol="responses") is None
    assert await responses_model_is_source_owned("source-only-model", scoped)


@pytest.mark.asyncio
async def test_eager_oauth_and_quota_detach_and_exhausted_owned_model(async_client):
    source_id = await create_source(async_client, "plan")
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        assert row is not None
        row.provider = "claude"
        row.auth_type = "claude_oauth"
        row.billing_mode = "subscription"
        row.entitlement_status = "verified"
        row.entitlement_verified_at = now
        row.extra_usage_enabled = False
        row.quota_checked_at = now
        row.models[0].subscription_eligible = True
        row.models[0].subscription_restricted = False
        row.oauth_credential = ModelSourceOAuthCredential(
            access_token_encrypted=b"encrypted", expires_at=now + timedelta(hours=1), account_uuid="principal"
        )
        row.quota_windows = [
            ModelSourceQuotaWindow(
                window_key="weekly", used_ratio=0.5, observed_at=now, resets_at=now + timedelta(days=1)
            )
        ]
        await session.commit()
    response = await async_client.post(
        "/api/api-keys/", json={"name": "plan", "billingPolicy": "subscription_only", "assignedSourceIds": [source_id]}
    )
    api_key = await validate_required_proxy_api_key_authorization(f"Bearer {response.json()['key']}")
    selected = await select_model_source("source-only-model", api_key, protocol="responses")
    assert selected is not None
    assert selected.oauth_credential is not None and selected.oauth_credential.account_uuid == "principal"
    assert selected.quota_windows[0].used_ratio == 0.5
    assert selected.models[0].subscription_eligible
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        assert row is not None
        row.quota_windows[0].used_ratio = 1
        await session.commit()
    assert await select_model_source("source-only-model", api_key, protocol="responses") is None
    assert await responses_model_is_source_owned("source-only-model", api_key)


@pytest.mark.asyncio
async def test_new_sessions_use_headroom_and_round_robin_not_name_order(async_client):
    first = await create_source(async_client, "alphabetically-first")
    second = await create_source(async_client, "alphabetically-last")
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionLocal() as session:
        for source_id, ratio in ((first, 0.8), (second, 0.1)):
            row = await session.get(ModelSource, source_id)
            assert row is not None
            row.billing_mode = "subscription"
            row.entitlement_status = "verified"
            row.entitlement_verified_at = now
            row.extra_usage_enabled = False
            row.quota_checked_at = now
            row.models[0].subscription_eligible = True
            row.models[0].subscription_restricted = False
            row.quota_windows = [
                ModelSourceQuotaWindow(
                    window_key="weekly", used_ratio=ratio, observed_at=now, resets_at=now + timedelta(days=1)
                )
            ]
        await session.commit()
    headroom = await select_model_source("source-only-model", None, protocol="responses")
    assert headroom is not None and headroom.id == second
    async with SessionLocal() as session:
        row = await session.get(ModelSource, second)
        assert row is not None
        row.quota_windows[0].used_ratio = 0.8
        await session.commit()
    async with SessionLocal() as session:
        row = await session.get(ModelSource, first)
        assert row is not None
        row.quota_windows[0].resets_at = now + timedelta(days=2)
        await session.commit()
    sooner_reset = await select_model_source("source-only-model", None, protocol="responses")
    assert sooner_reset is not None and sooner_reset.id == second
    async with SessionLocal() as session:
        row = await session.get(ModelSource, first)
        assert row is not None
        row.quota_windows[0].resets_at = now + timedelta(days=1)
        await session.commit()
    chosen = [await select_model_source("source-only-model", None, protocol="responses") for _ in range(4)]
    ids = [row.id for row in chosen if row is not None]
    assert len(ids) == 4
    assert set(ids) == {first, second}
    assert all(left != right for left, right in zip(ids, ids[1:]))


@pytest.mark.asyncio
async def test_uncertainty_fence_is_scoped_and_completed_requests_are_not_permanent_dedup(async_client):
    from sqlalchemy.exc import IntegrityError

    from app.db.models import ModelSourceRequestOperation

    source_id = await create_source(async_client, "fence")
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        assert source is not None
        source.max_concurrency = 1
        session.add(
            ModelSourceRequestOperation(
                id="first",
                request_fingerprint="same",
                api_key_scope="key-one",
                source_id=source_id,
                model="source-only-model",
                status="dispatching",
            )
        )
        await session.commit()
        session.add(
            ModelSourceRequestOperation(
                id="duplicate",
                request_fingerprint="same",
                api_key_scope="key-one",
                source_id=source_id,
                model="source-only-model",
                status="unknown",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        first = await session.get(ModelSourceRequestOperation, "first")
        assert first is not None
        first.status = "unknown"
        await session.commit()
        assert await select_model_source("source-only-model", None, protocol="responses") is not None
        # The fence concerns the originating request scope, not all client keys.
        session.add(
            ModelSourceRequestOperation(
                id="other-key",
                request_fingerprint="same",
                api_key_scope="key-two",
                source_id=source_id,
                model="source-only-model",
                status="dispatching",
            )
        )
        await session.commit()
        first.status = "completed"
        await session.commit()
        session.add(
            ModelSourceRequestOperation(
                id="next-turn",
                request_fingerprint="same",
                api_key_scope="key-one",
                source_id=source_id,
                model="source-only-model",
                status="dispatching",
            )
        )
        await session.commit()
        assert (await session.get(ModelSourceRequestOperation, "next-turn")).status == "dispatching"


@pytest.mark.asyncio
async def test_affinity_expires_and_evicts_old_sessions_without_widening_scope(async_client, monkeypatch):
    from types import SimpleNamespace

    import app.modules.model_sources.selection as selection

    clock = [100.0]
    monkeypatch.setattr(selection, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(selection, "AFFINITY_MAX_ENTRIES", 2)
    first = await create_source(async_client, "affinity-first", priority=10)
    second = await create_source(async_client, "affinity-second", priority=0)
    pinned = await select_model_source("source-only-model", None, protocol="responses", affinity_key="expiry")
    assert pinned is not None and pinned.id == first
    async with SessionLocal() as session:
        row = await session.get(ModelSource, second)
        assert row is not None
        row.priority = 20
        await session.commit()
    before_expiry = await select_model_source("source-only-model", None, protocol="responses", affinity_key="expiry")
    assert before_expiry is not None and before_expiry.id == first
    clock[0] += 301
    expired = await select_model_source("source-only-model", None, protocol="responses", affinity_key="expiry")
    assert expired is not None and expired.id == second
    async with SessionLocal() as session:
        row = await session.get(ModelSource, first)
        assert row is not None
        row.priority = 30
        await session.commit()
    target = await select_model_source("source-only-model", None, protocol="responses", affinity_key="eviction-target")
    assert target is not None and target.id == first
    async with SessionLocal() as session:
        row = await session.get(ModelSource, first)
        assert row is not None
        row.priority = 10
        await session.commit()
    for affinity in ("new-session-one", "new-session-two"):
        assert (
            await select_model_source("source-only-model", None, protocol="responses", affinity_key=affinity)
            is not None
        )
    evicted = await select_model_source("source-only-model", None, protocol="responses", affinity_key="eviction-target")
    assert evicted is not None and evicted.id == second


@pytest.mark.asyncio
@pytest.mark.parametrize("active_status,terminal_status", [("dispatching", "completed"), ("unknown", "rejected")])
async def test_source_delete_conflicts_only_for_active_work_and_preserves_history(
    async_client, active_status, terminal_status
):
    from app.db.models import ModelSourceRequestOperation

    source_id = await create_source(async_client, "delete-fence")
    created_key = await async_client.post("/api/api-keys/", json={"name": "scoped", "assignedSourceIds": [source_id]})
    assert created_key.status_code == 200
    async with SessionLocal() as session:
        session.add(
            ModelSourceRequestOperation(
                id="delete-operation",
                request_fingerprint="delete-fingerprint",
                api_key_scope="delete-key",
                source_id=source_id,
                model="source-only-model",
                status=active_status,
            )
        )
        await session.commit()
    blocked = await async_client.delete(f"/api/model-sources/{source_id}")
    assert blocked.status_code == 409
    async with SessionLocal() as session:
        assert await session.get(ModelSource, source_id) is not None
        operation = await session.get(ModelSourceRequestOperation, "delete-operation")
        assert operation is not None and operation.status == active_status
        operation.status = terminal_status
        await session.commit()
    deleted = await async_client.delete(f"/api/model-sources/{source_id}")
    assert deleted.status_code == 204
    async with SessionLocal() as session:
        assert await session.get(ModelSource, source_id) is None
        operation = await session.get(ModelSourceRequestOperation, "delete-operation")
        assert operation is not None
        assert operation.source_id == source_id and operation.status == terminal_status
    listed = await async_client.get("/api/api-keys/")
    key_data = next(row for row in listed.json() if row["id"] == created_key.json()["id"])
    assert key_data["sourceAssignmentScopeEnabled"] is True
    assert key_data["assignedSourceIds"] == []


@pytest.mark.asyncio
async def test_continuation_ownership_is_key_scoped_and_survives_source_deletion(async_client):
    from sqlalchemy.exc import IntegrityError

    from app.db.models import ModelSourceContinuationOwner

    source_id = await create_source(async_client, "continuation-owner")
    async with SessionLocal() as session:
        for scope in ("first-key", "second-key"):
            session.add(
                ModelSourceContinuationOwner(
                    api_key_scope=scope, token_hash="hashed-token", source_id=source_id, principal="verified-principal"
                )
            )
        await session.commit()
        session.add(
            ModelSourceContinuationOwner(
                api_key_scope="first-key",
                token_hash="hashed-token",
                source_id=source_id,
                principal="different-principal",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    deleted = await async_client.delete(f"/api/model-sources/{source_id}")
    assert deleted.status_code == 204
    async with SessionLocal() as session:
        for scope in ("first-key", "second-key"):
            owner = await session.get(ModelSourceContinuationOwner, (scope, "hashed-token"))
            assert owner is not None
            assert owner.source_id == source_id and owner.principal == "verified-principal"


@pytest.mark.asyncio
async def test_unknown_source_cost_keeps_budget_without_fabricating_host_price(async_client):
    from app.db.models import ApiKeyUsageReservation, LimitType
    from app.modules.api_keys.repository import ApiKeysRepository
    from app.modules.api_keys.service import ApiKeyRequestUsageBudget, ApiKeysService

    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "unknown-source-cost",
            "limits": [
                {"limitType": "cost_usd", "limitWindow": "weekly", "maxValue": 1_000_000_000},
                {"limitType": "input_tokens", "limitWindow": "weekly", "maxValue": 1_000_000},
            ],
        },
    )
    assert created.status_code == 200
    async with SessionLocal() as session:
        repository = ApiKeysRepository(session)
        service = ApiKeysService(repository)
        reservation = await service.enforce_limits_for_request(
            created.json()["id"],
            request_model="gpt-5.1",
            request_usage_budget=ApiKeyRequestUsageBudget(input_tokens=1024, output_tokens=1024),
        )
        assert reservation is not None
        limits = await repository.get_limits_by_key(created.json()["id"])
        reserved_cost = next(limit.current_value for limit in limits if limit.limit_type == LimitType.COST_USD)
        assert reserved_cost > 0
        await service.finalize_usage_reservation(
            reservation.reservation_id,
            model="gpt-5.1",
            input_tokens=20,
            output_tokens=4,
            preserve_reserved_cost_on_unknown_source=True,
        )
        limits = await repository.get_limits_by_key(created.json()["id"])
        assert next(limit.current_value for limit in limits if limit.limit_type == LimitType.COST_USD) == reserved_cost
        assert next(limit.current_value for limit in limits if limit.limit_type == LimitType.INPUT_TOKENS) == 20
        stored = await session.get(ApiKeyUsageReservation, reservation.reservation_id)
        assert stored is not None and stored.status == "finalized"
        assert stored.cost_microdollars is None


@pytest.mark.asyncio
async def test_concurrent_chat_selections_keep_most_restrictive_shared_group_limit(async_client):
    from app.modules.proxy.source_admission import try_claim

    first = await create_source(async_client, "shared-low-limit")
    second = await create_source(async_client, "shared-high-limit")
    for source_id, concurrency in ((first, 1), (second, 5)):
        response = await async_client.patch(f"/api/model-sources/{source_id}", json={
            "quotaGroup": "shared-plan", "maxConcurrency": concurrency,
        })
        assert response.status_code == 200
    # Both selectors complete before either non-awaiting admission claim.
    selected_first = await select_model_source(
        "source-only-model", None, protocol="responses", exclude_source_ids={second},
    )
    selected_second = await select_model_source(
        "source-only-model", None, protocol="responses", exclude_source_ids={first},
    )
    assert selected_first is not None and selected_second is not None
    first_claim = try_claim(selected_first)
    assert first_claim is not None
    second_claim = None
    try:
        second_claim = try_claim(selected_second)
        assert second_claim is None
    finally:
        first_claim.release_if_unowned()
        if second_claim is not None:
            second_claim.release_if_unowned()
