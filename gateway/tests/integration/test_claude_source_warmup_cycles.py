from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import ModelSource, ModelSourceLimitWarmup, ModelSourceRequestOperation
from app.db.session import SessionLocal
from app.modules.model_sources import subscription_runtime
from app.modules.model_sources import subscription_warmup as warmup
from tests.integration import test_claude_source_warmup as fixtures

pytestmark = pytest.mark.integration

warmup_db = fixtures.warmup_db
seed = fixtures.seed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "unknown"])
async def test_observed_reset_is_retained_when_provider_returns_idle_without_reset(warmup_db, monkeypatch, status):
    now = warmup_db
    monkeypatch.setattr(warmup, "utcnow", lambda: now)
    source_id = await seed(now)
    claimed = await warmup._claim(source_id)
    assert claimed is not None
    operation_id = claimed[1]
    real_reset = now + timedelta(seconds=1)
    async with SessionLocal() as session:
        attempt = await session.get(ModelSourceLimitWarmup, operation_id)
        attempt.attempted_at, attempt.status = now - timedelta(hours=6), status
        operation = await session.get(ModelSourceRequestOperation, operation_id)
        operation.status, operation.lease_expires_at = "completed" if status == "success" else "unknown", None
        source = await session.get(ModelSource, source_id)
        window = next(window for window in source.quota_windows if window.window_key == "five_hour")
        window.resets_at, window.observed_at = real_reset, now
        await session.commit()
    await warmup.observe_warmup_window(await subscription_runtime.load_source(source_id))
    source = await subscription_runtime.load_source(source_id)
    assert source.limit_warmups[0].reset_at == real_reset
    assert source.limit_warmups[0].status == ("window_started" if status == "success" else "unknown")
    assert await warmup._claim(source_id) is None  # provider-observed window is still active

    later = now + timedelta(seconds=2)
    monkeypatch.setattr(warmup, "utcnow", lambda: later)
    async with SessionLocal() as session:
        source = await session.get(ModelSource, source_id)
        source.quota_checked_at, source.entitlement_verified_at = later, later
        for window in source.quota_windows:
            window.observed_at = later
            if window.window_key == "five_hour":
                window.used_ratio, window.resets_at = 0, None
        await session.commit()
    # Current resets_at=None is not active, and the prior real provider boundary
    # is retained. Reusing the original permanent idle identity would deadlock.
    source = await subscription_runtime.load_source(source_id)
    assert warmup.warmup_unavailable_reason(source) is None
    next_claim = await warmup._claim(source_id)
    assert next_claim is not None and next_claim[1] != operation_id
    async with SessionLocal() as session:
        attempts = list(
            (
                await session.scalars(
                    select(ModelSourceLimitWarmup).where(
                        ModelSourceLimitWarmup.status != "skipped",
                    )
                )
            ).all()
        )
        assert len(attempts) == 2
        assert len({attempt.window_identity for attempt in attempts}) == 2


@pytest.mark.asyncio
async def test_elapsed_five_hours_without_provider_reset_cannot_release_unknown(warmup_db, monkeypatch):
    now = warmup_db
    source_id = await seed(now)
    claimed = await warmup._claim(source_id)
    assert claimed is not None
    async with SessionLocal() as session:
        attempt = await session.get(ModelSourceLimitWarmup, claimed[1])
        attempt.attempted_at, attempt.status = now - timedelta(hours=6), "unknown"
        operation = await session.get(ModelSourceRequestOperation, claimed[1])
        operation.status, operation.lease_expires_at = "unknown", None
        await session.commit()
    source = await subscription_runtime.load_source(source_id)
    assert warmup.warmup_unavailable_reason(source) == "acceptance_unknown"
    assert await warmup._claim(source_id) is None
