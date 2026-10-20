from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.models import ModelSourceLimitWarmup, ModelSourceRequestOperation, RequestLog
from app.db.session import SessionLocal
from app.modules.model_sources import anthropic, subscription_runtime
from app.modules.model_sources import subscription_warmup as warmup
from tests.integration import test_claude_source_warmup as fixtures

pytestmark = pytest.mark.integration
warmup_db = fixtures.warmup_db


@pytest.mark.asyncio
async def test_cancellation_during_success_finalization_preserves_completion_and_usage(warmup_db, monkeypatch):
    source_id = await fixtures.seed(warmup_db)
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_finish = warmup._finish
    refreshes = []

    async def dispatch(*args, **kwargs):
        return anthropic.NativeResponse(
            b"{}", 200, {}, anthropic.SourceUsage(input_tokens=7, output_tokens=1, served_model=fixtures.MODEL),
        )

    async def blocked_finish(*args, **kwargs):
        entered.set()
        await release.wait()
        await original_finish(*args, **kwargs)
        completed.set()

    async def refresh(source_id):
        refreshes.append(source_id)
        return await fixtures.no_refresh(source_id)

    monkeypatch.setattr(anthropic, "open_native", dispatch)
    monkeypatch.setattr(warmup, "_finish", blocked_finish)
    monkeypatch.setattr(subscription_runtime, "refresh_source_quota", refresh)
    task = asyncio.create_task(warmup.warm_source(source_id, manual=False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()  # Cancellation cannot interrupt the durable completion/usage write.
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert completed.is_set()
    assert refreshes == []  # Preserve cancellation after required finalization, before optional metadata I/O.
    async with SessionLocal() as session:
        assert (await session.scalars(select(ModelSourceRequestOperation.status))).one() == "completed"
        assert (await session.scalars(select(ModelSourceLimitWarmup.status))).one() == "success"
        log = (await session.scalars(select(RequestLog))).one()
        assert (log.input_tokens, log.output_tokens) == (7, 1)
        assert log.status == "success" and log.request_kind == "limit_warmup"
