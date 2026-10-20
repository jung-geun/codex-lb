"""Repair retained missing costs while preserving permanent folded history."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.usage.logs import (
    RequestLogLike,
    calculated_cost_from_log,
    calculated_source_cost_from_log,
    is_source_log,
)
from app.db.models import (
    AccountUsageRollup,
    AccountUsageRollupState,
    ApiKeyUsageRollup,
    ModelSource,
    ModelSourceModel,
    RequestDemandQuarterRollup,
    RequestLog,
    RequestReportHourlyRollup,
    RequestUsageHourlyRollup,
)
from app.db.session import sqlite_writer_section
from app.modules.accounts.usage_rollup import lock_fold_state
from app.modules.accounts.usage_time_rollup import CONVERSATION_WHITESPACE, epoch_seconds, to_dimension

_EXCLUDED_KINDS = ("warmup", "limit_warmup")
logger = logging.getLogger(__name__)


class _InconsistentFoldedCostBucket(RuntimeError):
    pass


@dataclass(frozen=True)
class BackfillBatch:
    scanned: int
    updated: int
    last_id: int


async def backfill_missing_costs(session: AsyncSession, *, after_id: int = 0, limit: int = 200) -> BackfillBatch:
    """Commit one bounded batch; NULL is the durable idempotency marker.

    Cursor progress is merely an optimization. Restarting at zero is safe.
    Never rebuild aggregate buckets from raw: retention may have pruned part
    of a bucket, and its permanent sums must survive intact.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    async with sqlite_writer_section():
        try:
            await lock_fold_state(session)
            state = await session.get(AccountUsageRollupState, 1, populate_existing=True)
            assert state is not None
            logs = (
                await session.scalars(
                    select(RequestLog)
                    .where(
                        RequestLog.id > after_id,
                        RequestLog.cost_usd.is_(None),
                        RequestLog.input_tokens.is_not(None),
                        or_(RequestLog.output_tokens.is_not(None), RequestLog.reasoning_tokens.is_not(None)),
                    )
                    .order_by(RequestLog.id)
                    .limit(limit)
                    .with_for_update()
                )
            ).all()
            source_keys = {(log.model_source_id, log.model) for log in logs if log.model_source_id is not None}
            source_models = {}
            if source_keys:
                source_models = {
                    (entry.source_id, entry.model): (entry, provider)
                    for entry, provider in (
                        await session.execute(
                            select(ModelSourceModel, ModelSource.provider)
                            .join(ModelSource, ModelSource.id == ModelSourceModel.source_id)
                            .where(tuple_(ModelSourceModel.source_id, ModelSourceModel.model).in_(source_keys))
                        )
                    ).all()
                }
            updated = 0
            last_id = logs[-1].id if logs else after_id
            for log in logs:
                if is_source_log(cast(RequestLogLike, log)):
                    matched = source_models.get((log.model_source_id, log.model))
                    overrides = None
                    if matched is not None and matched[1] == log.provider:
                        entry = matched[0]
                        overrides = {
                            name: getattr(entry, name)
                            for name in (
                                "input_per_1m", "cached_input_per_1m", "output_per_1m",
                                "cache_creation_per_1m", "cache_creation_1h_per_1m",
                            )
                        }
                    cost = calculated_source_cost_from_log(cast(RequestLogLike, log), overrides=overrides)
                else:
                    cost = calculated_cost_from_log(cast(RequestLogLike, log))
                if cost is None:
                    continue
                log_id = log.id
                try:
                    async with session.begin_nested():
                        await _mirror_cost(session, state, log, cost)
                        log.cost_usd = cost
                        await session.flush()
                except _InconsistentFoldedCostBucket:
                    logger.warning("Skipping inconsistent folded cost bucket for request log id=%d", log_id)
                    continue
                updated += 1
            await session.commit()
            return BackfillBatch(len(logs), updated, last_id)
        except BaseException:
            await session.rollback()
            raise


async def _mirror_cost(session: AsyncSession, state: AccountUsageRollupState, log: RequestLog, cost: float) -> None:
    normal = log.request_kind not in _EXCLUDED_KINDS
    if log.requested_at <= state.folded_through and normal:
        if log.api_key_id is not None:
            await session.execute(
                update(ApiKeyUsageRollup)
                .where(
                    ApiKeyUsageRollup.api_key_id == log.api_key_id,
                )
                .values(total_cost_usd=ApiKeyUsageRollup.total_cost_usd + cost)
            )
        if log.account_id is not None and log.deleted_at is None:
            # Only the latest visible duplicate contributes to account lifetime sums.
            latest = await session.scalar(
                select(func.max(RequestLog.id)).where(
                    RequestLog.account_id == log.account_id,
                    RequestLog.request_id == log.request_id,
                    RequestLog.requested_at == log.requested_at,
                    RequestLog.deleted_at.is_(None),
                    RequestLog.request_kind.not_in(_EXCLUDED_KINDS),
                )
            )
            if latest == log.id:
                await session.execute(
                    update(AccountUsageRollup)
                    .where(
                        AccountUsageRollup.account_id == log.account_id,
                    )
                    .values(total_cost_usd=AccountUsageRollup.total_cost_usd + cost)
                )
    epoch = epoch_seconds(log.requested_at)
    if log.requested_at < state.hourly_folded_through:
        await session.execute(
            update(RequestUsageHourlyRollup)
            .where(
                RequestUsageHourlyRollup.bucket_epoch == epoch // 3600 * 3600,
                RequestUsageHourlyRollup.account_id == to_dimension(log.account_id),
                RequestUsageHourlyRollup.api_key_id == to_dimension(log.api_key_id),
                RequestUsageHourlyRollup.model == log.model,
                RequestUsageHourlyRollup.service_tier == to_dimension(log.service_tier),
                RequestUsageHourlyRollup.request_kind == log.request_kind,
                RequestUsageHourlyRollup.is_deleted == (log.deleted_at is not None),
            )
            .values(
                cost_usd=RequestUsageHourlyRollup.cost_usd + cost, cost_count=RequestUsageHourlyRollup.cost_count + 1
            )
        )
        await session.execute(
            update(RequestDemandQuarterRollup)
            .where(
                RequestDemandQuarterRollup.slot_epoch == epoch // 900 * 900,
                RequestDemandQuarterRollup.account_id == to_dimension(log.account_id),
                RequestDemandQuarterRollup.api_key_id == to_dimension(log.api_key_id),
                RequestDemandQuarterRollup.model == log.model,
                RequestDemandQuarterRollup.reasoning_effort == to_dimension(log.reasoning_effort),
                RequestDemandQuarterRollup.request_kind == log.request_kind,
                RequestDemandQuarterRollup.status == log.status,
                RequestDemandQuarterRollup.is_deleted == (log.deleted_at is not None),
            )
            .values(cost_usd=RequestDemandQuarterRollup.cost_usd + cost)
        )
    if log.requested_at < state.reports_folded_through and normal and log.source != "limit_warmup":
        conversation = (log.conversation_id or "").strip(CONVERSATION_WHITESPACE) or None
        result = await session.execute(
            update(RequestReportHourlyRollup)
            .where(
                RequestReportHourlyRollup.bucket_epoch == epoch // 3600 * 3600,
                RequestReportHourlyRollup.account_id == to_dimension(log.account_id),
                RequestReportHourlyRollup.model_source_id == to_dimension(log.model_source_id),
                RequestReportHourlyRollup.account_type == (
                    "codex" if log.model_source_id is None else log.provider or "other"
                ),
                RequestReportHourlyRollup.api_key_id == to_dimension(log.api_key_id),
                RequestReportHourlyRollup.model == log.model,
                RequestReportHourlyRollup.useragent_group == to_dimension(log.useragent_group),
                RequestReportHourlyRollup.conversation_id == to_dimension(conversation),
                RequestReportHourlyRollup.unknown_cost_count > 0,
            )
            .values(
                cost_usd=RequestReportHourlyRollup.cost_usd + cost,
                unknown_cost_count=RequestReportHourlyRollup.unknown_cost_count - 1,
            )
        )
        if cast(CursorResult, result).rowcount != 1:
            raise _InconsistentFoldedCostBucket("Missing or inconsistent folded report cost bucket; row rolled back")
