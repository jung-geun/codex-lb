"""Synthetic native peers exercise warmup safety; never contact Claude."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from sqlalchemy import select

from app.core.crypto import TokenEncryptor
from app.db.models import (
    ModelSource,
    ModelSourceLimitWarmup,
    ModelSourceModel,
    ModelSourceOAuthCredential,
    ModelSourceQuotaWindow,
    ModelSourceRequestOperation,
    RequestLog,
)
from app.db.session import SessionLocal
from app.modules.model_sources import anthropic, subscription_runtime, subscription_scheduler
from app.modules.model_sources import subscription_warmup as warmup
from app.modules.model_sources.providers.base import CatalogModel, ProviderSnapshot, QuotaWindow
from app.modules.model_sources.service import _to_response

pytestmark = pytest.mark.integration
MODEL = "claude-haiku-entitled"


@pytest.fixture
async def warmup_db(db_setup, monkeypatch):
    @asynccontextmanager
    async def background():
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(warmup, "get_background_session", background)
    monkeypatch.setattr(subscription_runtime, "get_background_session", background)
    monkeypatch.setattr(warmup.AuditService, "log_async", lambda *args, **kwargs: None)
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


async def seed(now, source_id="warm-a", *, principal="principal", group=None, weekly=0.2, reset=None):
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        source = ModelSource(
            id=source_id,
            name=source_id,
            kind="openai_compatible",
            provider="claude",
            auth_type="claude_oauth",
            base_url="https://api.anthropic.com",
            billing_mode="subscription",
            supports_messages=True,
            routing_policy="normal",
            is_enabled=True,
            limit_warmup_enabled=True,
            quota_group=group,
            entitlement_status="verified",
            entitlement_verified_at=now,
            extra_usage_enabled=False,
            quota_checked_at=now,
            max_concurrency=1,
            oauth_credential=ModelSourceOAuthCredential(
                access_token_encrypted=encryptor.encrypt("test-access-" + source_id),
                expires_at=now + timedelta(hours=1),
                account_uuid=principal,
                organization_uuid="org",
            ),
            models=[
                ModelSourceModel(
                    model=MODEL, upstream_model=MODEL, subscription_eligible=True, subscription_restricted=False
                )
            ],
            quota_windows=[
                ModelSourceQuotaWindow(window_key="five_hour", used_ratio=0, observed_at=now, resets_at=reset),
                ModelSourceQuotaWindow(
                    window_key="seven_day", used_ratio=weekly, observed_at=now, resets_at=now + timedelta(days=2)
                ),
            ],
        )
        session.add(source)
        await session.commit()
    return source_id


async def no_refresh(source_id):
    return _to_response(await subscription_runtime.load_source(source_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ratio,reset_delta,observed_delta,expected",
    [
        (0, None, 0, None),  # idle, no invented active 5h clock
        (0, -60, 0, None),  # freshly observed replenishment
        (0, 60, 0, "five_hour_active"),  # zero use is not an idle active window
        (0.1, None, 0, "five_hour_not_idle"),
        (0, -60, -120, "five_hour_not_idle"),  # crossing a clock is not replenishment
        (1, -60, 0, "five_hour_not_idle"),
    ],
)
async def test_only_idle_or_observed_reset_is_warmable(warmup_db, ratio, reset_delta, observed_delta, expected):
    now = warmup_db
    source_id = await seed(now, reset=now + timedelta(seconds=reset_delta) if reset_delta is not None else None)
    async with SessionLocal() as session:
        row = await session.get(ModelSourceQuotaWindow, (source_id, "five_hour"))
        row.used_ratio, row.observed_at = ratio, now + timedelta(seconds=observed_delta)
        await session.commit()
    source = await subscription_runtime.load_source(source_id)
    assert warmup.warmup_unavailable_reason(source) == expected


@pytest.mark.asyncio
async def test_weekly_exhaustion_is_persisted_skip_without_any_dispatch(warmup_db, monkeypatch):
    source_id = await seed(warmup_db, weekly=1)

    async def forbidden(*args, **kwargs):
        pytest.fail("exhausted weekly quota must never dispatch or refresh inference")

    monkeypatch.setattr(anthropic, "open_native", forbidden)
    result = await warmup.warm_source(source_id, manual=False)
    assert result.warmup_unavailable_reason == "weekly_exhausted"
    assert result.limit_warmup.status == "skipped"
    assert result.limit_warmup.error_message == "weekly_exhausted"
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceRequestOperation))).all() == []
        assert (await session.scalars(select(RequestLog))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("same_principal", [True, False])
async def test_concurrent_group_claims_deduplicate_across_independent_sessions(warmup_db, monkeypatch, same_principal):
    # Disable the process mutex: database serialization, not one Python event
    # loop, must enforce the cross-replica single attempt.
    @asynccontextmanager
    async def no_mutex():
        yield

    monkeypatch.setattr(warmup, "sqlite_writer_section", no_mutex)
    a = await seed(warmup_db, "warm-a", group="shared")
    b = await seed(warmup_db, "warm-b", principal="principal" if same_principal else "other", group="shared")
    results = await asyncio.gather(warmup._claim(a), warmup._claim(b))
    assert sum(result is not None for result in results) == 1
    async with SessionLocal() as session:
        operations = list((await session.scalars(select(ModelSourceRequestOperation))).all())
        assert len(operations) == 1 and operations[0].status == "dispatching"
        attempts = list((await session.scalars(select(ModelSourceLimitWarmup))).all())
        assert sum(attempt.status == "dispatching" for attempt in attempts) == 1


@pytest.mark.asyncio
async def test_group_busy_and_paused_optout_stale_extra_usage_are_not_dispatched(warmup_db):
    source_id = await seed(warmup_db)
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.is_enabled = False
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[0].error_message == "disabled"
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.is_enabled, source.limit_warmup_enabled = True, False
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[0].error_message == "warmup_disabled"
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.limit_warmup_enabled, source.extra_usage_enabled = True, True
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[
        0
    ].error_message == "extra_usage_not_disabled"
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.extra_usage_enabled = False
        source.entitlement_verified_at = warmup_db - timedelta(minutes=6)
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[0].error_message == "entitlement_stale"
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.entitlement_verified_at = warmup_db
        session.add(
            ModelSourceRequestOperation(
                id="user-operation",
                request_fingerprint="user",
                api_key_scope="user",
                source_id=source_id,
                model=MODEL,
                status="dispatching",
                lease_expires_at=warmup_db + timedelta(seconds=90),
            )
        )
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[0].error_message == "capacity_busy"


@pytest.mark.asyncio
async def test_actual_native_dispatch_is_pinned_minimal_and_records_usage(warmup_db, monkeypatch):
    source_id = await seed(warmup_db)
    await seed(warmup_db, "paid-api-decoy", principal="different")
    async with SessionLocal() as session:
        decoy = await session.get(ModelSource, "paid-api-decoy")
        decoy.provider, decoy.auth_type, decoy.billing_mode, decoy.priority = (
            "anthropic",
            "api_key",
            "api_metered",
            1000,
        )
        await session.commit()
    calls = []

    async def messages(request):
        calls.append(
            (request.path, {key.lower(): value for key, value in request.headers.items()}, await request.json())
        )
        return web.json_response(
            {
                "id": "msg_warm",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 9, "output_tokens": 1, "cache_read_input_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    requested_urls = []

    @asynccontextmanager
    async def lease():
        async with aiohttp.ClientSession() as session:

            class SyntheticPeer:
                def post(self, url, **kwargs):
                    requested_urls.append(url)
                    return session.post(f"http://127.0.0.1:{port}/v1/messages", **kwargs)

            yield SyntheticPeer()

    monkeypatch.setattr(anthropic, "lease_model_source_session", lease)
    monkeypatch.setattr(subscription_runtime, "refresh_source_quota", no_refresh)
    try:
        result = await warmup.warm_source(source_id, manual=False)
    finally:
        await runner.cleanup()
    assert result.limit_warmup.status == "success"  # response != observed running window
    assert result.limit_warmup.reset_at is None
    assert requested_urls == ["https://api.anthropic.com/v1/messages"]
    assert calls[0][1]["authorization"] == "Bearer test-access-warm-a"
    assert calls[0][2] == {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": 1,
        "stream": False,
    }
    async with SessionLocal() as session:
        log = (await session.scalars(select(RequestLog))).one()
        assert log.model_source_id == source_id and log.provider == "claude"
        assert (log.input_tokens, log.output_tokens, log.cached_input_tokens) == (11, 1, 2)
        assert log.request_kind == "limit_warmup" and log.cost_usd is None
        assert log.cost_kind == "api_equivalent"  # absent rates are unknown, never free
        assert (await session.scalars(select(ModelSourceRequestOperation.status))).one() == "completed"
    await warmup.warm_source(source_id, manual=False)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "server", "explicit_rejection"])
async def test_ambiguous_and_rejected_operations_never_retry_same_window(warmup_db, monkeypatch, failure):
    source_id = await seed(warmup_db)
    seen = []

    async def dispatch(source, payload, **kwargs):
        seen.append(source.id)
        if failure == "timeout":
            raise TimeoutError()
        raise anthropic.NativeRejected(
            anthropic.NativeResponse(b"{}", 429 if failure == "explicit_rejection" else 500, {})
        )

    monkeypatch.setattr(anthropic, "open_native", dispatch)
    monkeypatch.setattr(subscription_runtime, "refresh_source_quota", no_refresh)
    result = await warmup.warm_source(source_id, manual=False)
    expected = "rejected" if failure == "explicit_rejection" else "unknown"
    assert result.limit_warmup.status == expected
    await warmup.warm_source(source_id, manual=False)
    assert seen == [source_id]
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceRequestOperation.status))).one() == expected
        log = (await session.scalars(select(RequestLog))).one()
        assert log.input_tokens is None and log.output_tokens is None


@pytest.mark.asyncio
async def test_provider_refresh_observation_alone_marks_started_window(warmup_db, monkeypatch):
    source_id = await seed(warmup_db)

    async def dispatch(*args, **kwargs):
        return anthropic.NativeResponse(b"{}", 200, {}, anthropic.SourceUsage(input_tokens=5, output_tokens=1))

    async def inspect(token):
        now = datetime.now(UTC).replace(tzinfo=None)
        return ProviderSnapshot(
            billing_mode="subscription",
            entitlement_status="verified",
            extra_usage_enabled=False,
            account_uuid="principal",
            organization_uuid="org",
            windows=[
                QuotaWindow("five_hour", used_ratio=0.001, resets_at=now + timedelta(hours=5)),
                QuotaWindow("seven_day", used_ratio=0.2, resets_at=now + timedelta(days=2)),
            ],
            models=[CatalogModel(model=MODEL, subscription_eligible=True)],
        )

    monkeypatch.setattr(anthropic, "open_native", dispatch)
    monkeypatch.setattr(subscription_runtime.claude, "inspect", inspect)
    result = await warmup.warm_source(source_id, manual=False)
    assert result.limit_warmup.status == "window_started"
    assert result.limit_warmup.reset_at > warmup_db
    assert result.warmup_unavailable_reason == "five_hour_active"


@pytest.mark.asyncio
async def test_leader_cycle_refreshes_before_warmup(monkeypatch):
    calls = []

    async def refresh():
        calls.append("refresh")

    async def warm():
        calls.append("warmup")

    monkeypatch.setattr(subscription_scheduler, "refresh_subscription_sources", refresh)
    monkeypatch.setattr(subscription_scheduler, "warm_subscription_sources", warm)
    assert await subscription_scheduler.SubscriptionRefreshScheduler()._refresh_as_leader()
    assert calls == ["refresh", "warmup"]


@pytest.mark.asyncio
async def test_exhausted_model_quota_and_shared_cooldown_cannot_warm(warmup_db):
    source_id = await seed(warmup_db)
    peer_id = await seed(warmup_db, "same-principal-peer")
    async with SessionLocal() as session:
        session.add(
            ModelSourceQuotaWindow(
                source_id=peer_id,
                window_key="seven_day_haiku:" + MODEL,
                model=MODEL,
                used_ratio=1,
                observed_at=warmup_db,
                resets_at=warmup_db + timedelta(days=1),
            )
        )
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[
        0
    ].error_message == "model_quota_unavailable"
    async with SessionLocal() as session:
        peer = await session.get(ModelSource, peer_id)
        peer.cooldown_until = warmup_db + timedelta(minutes=1)
        await session.commit()
    assert await warmup._claim(source_id) is None
    assert (await subscription_runtime.load_source(source_id)).limit_warmups[0].error_message == "cooldown"
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceRequestOperation))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["is_enabled", "limit_warmup_enabled", "model_restriction"])
async def test_pause_optout_or_model_restriction_after_claim_cannot_dispatch(warmup_db, monkeypatch, changed):
    source_id = await seed(warmup_db)
    original_claim = warmup._claim

    async def claim_then_change(source_id):
        claimed = await original_claim(source_id)
        async with SessionLocal() as session:
            source = await session.get(ModelSource, source_id)
            if changed == "model_restriction":
                source.models[0].subscription_restricted = True
            else:
                setattr(source, changed, False)
            await session.commit()
        return claimed

    @asynccontextmanager
    async def forbidden_lease():
        pytest.fail("stale selected source must be rejected before opening native transport")
        yield

    monkeypatch.setattr(warmup, "_claim", claim_then_change)
    monkeypatch.setattr(anthropic, "lease_model_source_session", forbidden_lease)
    monkeypatch.setattr(subscription_runtime, "refresh_source_quota", no_refresh)
    result = await warmup.warm_source(source_id, manual=False)
    assert result.limit_warmup.status == "rejected"
    assert result.limit_warmup.error_message == "admission_changed"
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceRequestOperation.status))).one() == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", ["weekly_exhausted", "five_hour_active", "refresh_failed"])
async def test_manual_warmup_refreshes_metadata_before_admission(warmup_db, monkeypatch, observed):
    source_id = await seed(warmup_db)
    async with SessionLocal() as session:
        row = await session.get(ModelSource, source_id)
        row.quota_checked_at = row.entitlement_verified_at = warmup_db - timedelta(minutes=6)
        await session.commit()
    inspections = []

    async def inspect(token):
        inspections.append(token)
        if observed == "refresh_failed":
            raise subscription_runtime.ProviderError("Native quota unavailable")
        now = datetime.now(UTC).replace(tzinfo=None)
        return ProviderSnapshot(
            billing_mode="subscription",
            entitlement_status="verified",
            extra_usage_enabled=False,
            account_uuid="principal",
            organization_uuid="org",
            windows=[
                QuotaWindow(
                    "five_hour",
                    used_ratio=0,
                    resets_at=now + timedelta(hours=5) if observed == "five_hour_active" else None,
                ),
                QuotaWindow(
                    "seven_day",
                    used_ratio=1 if observed == "weekly_exhausted" else 0.2,
                    resets_at=now + timedelta(days=2),
                ),
            ],
            models=[CatalogModel(model=MODEL, subscription_eligible=True)],
        )

    async def forbidden_dispatch(*args, **kwargs):
        pytest.fail("manual metadata refresh must not bypass newly observed quota or refresh errors")

    monkeypatch.setattr(subscription_runtime.claude, "inspect", inspect)
    monkeypatch.setattr(anthropic, "open_native", forbidden_dispatch)
    result = await warmup.warm_source(source_id, manual=True)
    reason = "entitlement_stale" if observed == "refresh_failed" else observed
    assert inspections == ["test-access-" + source_id]
    assert result.warmup_unavailable_reason == reason
    if observed == "refresh_failed":
        # A failed quota read cannot refresh old entitlement authority. Its
        # retained safe error is still exposed alongside that stronger gate.
        assert result.entitlement_verified_at == warmup_db - timedelta(minutes=6)
        assert result.quota_error == "Native quota unavailable"
    assert result.limit_warmup.status == "skipped" and result.limit_warmup.error_message == reason
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceRequestOperation))).all() == []
