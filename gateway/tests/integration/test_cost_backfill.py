from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.core.usage.logs import calculated_cost_from_log
from app.db.models import (
    AccountUsageRollup,
    AccountUsageRollupState,
    ApiKey,
    ApiKeyUsageRollup,
    ModelSource,
    ModelSourceModel,
    RequestDemandQuarterRollup,
    RequestLog,
    RequestReportHourlyRollup,
    RequestUsageHourlyRollup,
)
from app.db.session import SessionLocal
from app.modules.accounts.usage_rollup import run_fold_pass
from app.modules.accounts.usage_time_rollup import run_hourly_fold_pass
from app.modules.reports.rollup import run_report_fold_pass
from app.modules.request_logs.cost_backfill import backfill_missing_costs
from tests.integration.test_account_usage_rollup import _make_account

pytestmark = pytest.mark.integration
BASE = datetime(2026, 9, 8, 10)
NOW = BASE + timedelta(hours=4)


async def seed():
    async with SessionLocal() as session:
        session.add(_make_account("a", "a@example.com"))
        session.add(ApiKey(id="k", name="test", key_hash="hash", key_prefix="key"))
        await session.flush()
        variants = [
            {},
            {"service_tier": "priority"},
            {"service_tier": "flex"},
            {"deleted_at": BASE},
            {"request_kind": "warmup"},
            {"source": "limit_warmup"},
            {"request_id": "duplicate"},
            {"request_id": "duplicate"},
            {"account_id": None},
            {"output_tokens": None, "reasoning_tokens": 100},
            {"model": "gpt-6-astra-2026-09-04"},
            {"model": "unknown"},
            {"model_source_id": "external"},
            {"model_source_kind": "openai_compatible"},
            {"cost_usd": 0.0},
            {"cost_usd": 7.0, "request_id": "pruned"},
            {"requested_at": NOW, "request_id": "live"},
        ]
        for i, variant in enumerate(variants):
            values: dict[str, object] = dict(
                account_id="a",
                api_key_id="k",
                request_id=f"r{i}",
                requested_at=BASE,
                model="gpt-6-astra",
                input_tokens=1000,
                output_tokens=100,
                cached_input_tokens=500,
                status="success",
                request_kind="normal",
                conversation_id=" \tconversation\n",
                useragent_group="\x1fagent",
            )
            values.update(variant)
            session.add(RequestLog(**values))
        await session.commit()


async def fold():
    await run_fold_pass(now=NOW)
    await run_hourly_fold_pass(now=NOW)
    await run_report_fold_pass(now=NOW)


async def test_backfill_preserves_folded_history_filters_and_idempotency(async_client):
    await seed()
    await fold()
    async with SessionLocal() as session:
        before_counts = {
            table: await session.scalar(select(func.sum(table.request_count)))
            for table in [
                AccountUsageRollup,
                ApiKeyUsageRollup,
                RequestUsageHourlyRollup,
                RequestDemandQuarterRollup,
                RequestReportHourlyRollup,
            ]
        }
        await session.execute(delete(RequestLog).where(RequestLog.request_id == "pruned"))
        await session.commit()
    cursor = 0
    updated = 0
    while True:
        async with SessionLocal() as session:
            batch = await backfill_missing_costs(session, after_id=cursor, limit=3)
        updated += batch.updated
        cursor = batch.last_id
        if batch.scanned < 3:
            break
    assert updated == 12
    async with SessionLocal() as session:
        assert (await backfill_missing_costs(session)).updated == 0
        logs = (await session.scalars(select(RequestLog).order_by(RequestLog.id))).all()
        for log in logs:
            if log.model == "unknown" or log.model_source_id or log.model_source_kind == "openai_compatible":
                assert log.cost_usd is None
            else:
                assert log.cost_usd is not None
        folded = [log for log in logs if log.requested_at < NOW]

        def cost(rows):
            return sum(log.cost_usd or 0 for log in rows)

        normal = [log for log in folded if log.request_kind not in ("warmup", "limit_warmup")]
        account_rows = {
            (log.account_id, log.request_id, log.requested_at): log
            for log in normal
            if log.account_id is not None and log.deleted_at is None
        }
        assert await session.scalar(select(AccountUsageRollup.total_cost_usd)) == pytest.approx(
            7 + cost(account_rows.values())
        )
        assert await session.scalar(select(ApiKeyUsageRollup.total_cost_usd)) == pytest.approx(7 + cost(normal))
        for table in [RequestUsageHourlyRollup, RequestDemandQuarterRollup]:
            assert await session.scalar(select(func.sum(table.cost_usd))) == pytest.approx(7 + cost(folded))
        assert await session.scalar(select(func.sum(RequestUsageHourlyRollup.cost_count))) == 1 + sum(
            log.cost_usd is not None for log in folded
        )
        assert await session.scalar(select(func.sum(RequestReportHourlyRollup.cost_usd))) == pytest.approx(
            7 + cost([log for log in normal if log.source != "limit_warmup"])
        )
        for table, count in before_counts.items():
            assert await session.scalar(select(func.sum(table.request_count))) == count
    response = await async_client.get("/api/request-logs?limit=100")
    assert response.status_code == 200
    rows = response.json()["requests"]
    astra = next(row for row in rows if row["requestId"] == "r0")
    assert astra["costUsd"] == pytest.approx(0.0105)


async def test_failed_mirror_rolls_back_raw_and_all_aggregates(db_setup, monkeypatch):
    await seed()
    await fold()
    import app.modules.request_logs.cost_backfill as module

    original = module._mirror_cost

    async def fail_after_mirror(*args):
        await original(*args)
        raise RuntimeError("interrupted repair")

    monkeypatch.setattr(module, "_mirror_cost", fail_after_mirror)
    async with SessionLocal() as session:
        with pytest.raises(RuntimeError, match="interrupted repair"):
            await backfill_missing_costs(session)
    async with SessionLocal() as session:
        assert await session.scalar(select(RequestLog.cost_usd).where(RequestLog.request_id == "r0")) is None
        assert await session.scalar(select(AccountUsageRollup.total_cost_usd)) == 7.0
        assert await session.scalar(select(func.sum(RequestUsageHourlyRollup.cost_usd))) == 7.0


async def test_watermark_equality_is_lifetime_inclusive_and_hourly_exclusive(db_setup):
    await seed()
    await fold()
    async with SessionLocal() as session:
        state = await session.get(AccountUsageRollupState, 1)
        assert state is not None
        at = state.hourly_folded_through
        session.add(
            RequestLog(
                account_id="a",
                request_id="boundary",
                requested_at=at,
                model="gpt-6-astra",
                input_tokens=1000,
                output_tokens=100,
                status="success",
            )
        )
        await session.commit()
    # Fold lifetime to include the equality row, while hourly stays at the same hour.
    await run_fold_pass(now=NOW)
    async with SessionLocal() as session:
        await backfill_missing_costs(session)
        boundary = await session.scalar(select(RequestLog).where(RequestLog.request_id == "boundary"))
        assert boundary is not None
        assert boundary.cost_usd == calculated_cost_from_log(boundary)


async def test_source_repair_preserves_deleted_pruned_provider_dimensions_and_unknown_usage(db_setup):
    async with SessionLocal() as session:
        session.add(_make_account("a", "a@example.com"))
        session.add(ApiKey(id="k", name="test", key_hash="hash", key_prefix="key"))
        source = ModelSource(
            id="a", name="Synthetic source", provider="claude", kind="anthropic_compatible",
            base_url="https://api.anthropic.com", billing_mode="subscription",
            models=[ModelSourceModel(model="claude-opus-5-5", input_per_1m=0.0, output_per_1m=99.0)],
        )
        session.add(source)
        await session.flush()
        variants = [
            {"request_id": "source"},
            {"request_id": "deleted-source", "model_source_id": "deleted", "deleted_at": BASE},
            {"request_id": "other-provider", "provider": "other"},
            {"request_id": "codex-collision", "account_id": "a", "model_source_id": None,
             "model_source_kind": None, "provider": "codex"},
            {"request_id": "missing-usage", "input_tokens": None, "output_tokens": None, "status": "error"},
            {"request_id": "missing-cache", "cached_input_tokens": None},
            {"request_id": "unknown-ttl", "cache_creation_5m_input_tokens": None},
            {"request_id": "known-zero", "cost_usd": 0.0},
            {"request_id": "pruned", "cost_usd": 10.0},
            {"request_id": "live", "requested_at": NOW},
            {"request_id": "review", "model_source_id": None, "model_source_kind": None,
             "provider": "codex", "model": "codex-auto-review", "served_model": None},
        ]
        for variant in variants:
            values = dict(
                account_id=None, api_key_id="k", model_source_id="a", provider="claude",
                model_source_kind="anthropic_compatible", model="claude-opus-5-5",
                served_model="claude-opus-5-5", requested_at=BASE, status="success", request_kind="normal",
                input_tokens=1000, output_tokens=100, cached_input_tokens=200,
                cache_creation_input_tokens=500, cache_creation_5m_input_tokens=400,
                cache_creation_1h_input_tokens=100, useragent_group="OMP", conversation_id=" thread ",
            )
            values.update(variant)
            session.add(RequestLog(**values))
        await session.commit()
    await fold()
    async with SessionLocal() as session:
        await session.execute(delete(RequestLog).where(RequestLog.request_id == "pruned"))
        await session.commit()
        batch = await backfill_missing_costs(session, limit=100)
        assert batch.updated == 3  # source, deleted-source, and the unfurled live row
        rows = {row.request_id: row for row in (await session.scalars(select(RequestLog))).all()}
        configured = (200 * 0.2 + 400 * 5 + 100 * 8 + 100 * 99) / 1_000_000
        published = (300 * 4 + 200 * 0.2 + 400 * 5 + 100 * 8 + 100 * 20) / 1_000_000
        assert rows["source"].cost_usd == pytest.approx(configured)
        assert rows["deleted-source"].cost_usd == pytest.approx(published)
        assert rows["live"].cost_usd == pytest.approx(configured)
        assert rows["known-zero"].cost_usd == 0
        for key in ("other-provider", "codex-collision", "missing-usage", "missing-cache", "unknown-ttl", "review"):
            assert rows[key].cost_usd is None
        for table in (RequestUsageHourlyRollup, RequestDemandQuarterRollup, RequestReportHourlyRollup):
            assert await session.scalar(select(func.sum(table.cost_usd))) == pytest.approx(10 + configured + published)
        assert await session.scalar(select(ApiKeyUsageRollup.total_cost_usd)) == pytest.approx(
            10 + configured + published
        )
        reports = (await session.scalars(select(RequestReportHourlyRollup))).all()
        # Source-id and native-account-id collisions, and provider collisions,
        # remain independent report primary keys despite otherwise equal dims.
        other = [row for row in reports if row.account_type == "other"]
        assert len(other) == 1 and other[0].unknown_cost_count == 1 and other[0].cost_usd == 0
        source_rows = [row for row in reports if row.account_type == "claude"]
        assert sum(row.unknown_cost_count for row in source_rows) == 3
        assert sum(row.unknown_cost_count for row in reports) == 6
        assert (await backfill_missing_costs(session, limit=100)).updated == 0
        assert await session.scalar(select(func.sum(RequestReportHourlyRollup.cost_usd))) == pytest.approx(
            10 + configured + published
        )


async def test_inconsistent_report_bucket_preserves_unknown_cost_and_allows_later_repair(db_setup):
    await seed()
    await fold()
    async with SessionLocal() as session:
        await session.execute(delete(RequestReportHourlyRollup))
        await session.commit()
        batch = await backfill_missing_costs(session)
        assert batch.updated == 3  # warm-up, report-excluded source, and unfurled live tail
        assert await session.scalar(select(RequestLog.cost_usd).where(RequestLog.request_id == "r0")) is None
        live_cost = await session.scalar(select(RequestLog.cost_usd).where(RequestLog.request_id == "live"))
        assert live_cost == pytest.approx(0.0105)
        # The report-excluded source row still belongs to account/key lifetime usage.
        assert await session.scalar(select(AccountUsageRollup.total_cost_usd)) == pytest.approx(7.0105)
        assert await session.scalar(select(ApiKeyUsageRollup.total_cost_usd)) == pytest.approx(7.0105)
        assert await session.scalar(select(func.sum(RequestUsageHourlyRollup.cost_usd))) == pytest.approx(7.021)
        assert await session.scalar(select(func.sum(RequestDemandQuarterRollup.cost_usd))) == pytest.approx(7.021)
        assert (await backfill_missing_costs(session, after_id=batch.last_id)).scanned == 0
