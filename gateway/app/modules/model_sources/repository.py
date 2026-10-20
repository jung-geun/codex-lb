from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import case, delete, extract, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.usage.logs import CANCELLED_STATUS, NON_ERROR_STATUSES, SUCCESS_STATUS
from app.core.utils.time import utcnow
from app.db.models import (
    ModelSource,
    ModelSourceModel,
    ModelSourceQuotaObservation,
    ModelSourceRequestOperation,
    RequestLog,
)
from app.modules.model_sources.schemas import ModelSourceUsageResponse
from app.modules.request_logs.repository import RequestLogsRepository

if TYPE_CHECKING:
    from app.modules.model_sources.selection import SourceProtocol


class ModelSourceHasActiveOperationsError(ValueError):
    pass


class ModelSourcesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_sources(self) -> list[ModelSource]:
        result = await self._session.execute(
            select(ModelSource)
            .options(
                selectinload(ModelSource.models),
                selectinload(ModelSource.oauth_credential),
                selectinload(ModelSource.quota_windows),
            )
            .order_by(ModelSource.priority.desc(), ModelSource.name, ModelSource.id)
        )
        return list(result.scalars().unique().all())

    async def list_enabled_sources(self) -> list[ModelSource]:
        return [source for source in await self.list_sources() if source.is_enabled]

    async def get_by_id(self, source_id: str) -> ModelSource | None:
        result = await self._session.execute(
            select(ModelSource)
            .options(
                selectinload(ModelSource.models),
                selectinload(ModelSource.oauth_credential),
                selectinload(ModelSource.quota_windows),
            )
            .where(ModelSource.id == source_id)
        )
        return result.scalar_one_or_none()

    async def quota_history(
        self, source_id: str, *, window_start: datetime, window_end: datetime
    ) -> list[ModelSourceQuotaObservation]:
        table = ModelSourceQuotaObservation
        # Keep the newest real sample per window/hour, with no interpolation.
        # The source/time index bounds this to at most thirty retained days.
        ranked = select(
            table.id,
            func.row_number().over(
                partition_by=(table.window_key, table.model, func.floor(extract("epoch", table.observed_at) / 3600)),
                order_by=(table.observed_at.desc(), table.id.desc()),
            ).label("sample_rank"),
        ).where(
            table.source_id == source_id,
            table.observed_at >= window_start,
            table.observed_at <= window_end,
        ).subquery()
        result = await self._session.scalars(
            select(table).where(
                table.id.in_(select(ranked.c.id).where(ranked.c.sample_rank == 1))
            ).order_by(table.observed_at, table.window_key)
        )
        return list(result.all())

    async def quota_history_component_ids(
        self, source_id: str, *, previous_source: ModelSource | None = None,
    ) -> set[str]:
        """All aliases joined transitively by native principal OR quota group.

        Disabled sources still own historical observations. Keep principal and
        operator-label namespaces distinct, and do not infer an identity from
        an unverified subscription that has no native principal.
        A guarded native writer may supply its old root snapshot after locking
        the parent with a write, avoiding a SQLite read-to-write upgrade.
        """
        aliases: dict[str, list[tuple[str, ...]]] = {}
        members: dict[tuple[str, ...], list[str]] = {}
        for current in await self.list_sources():
            source = previous_source if previous_source is not None and current.id == source_id else current
            keys: list[tuple[str, ...]] = []
            credential = source.oauth_credential
            if credential is not None and credential.account_uuid:
                keys.append(("principal", source.provider, credential.organization_uuid or "", credential.account_uuid))
            if source.quota_group:
                keys.append(("quota_group", source.quota_group))
            aliases[source.id] = keys
            for key in keys:
                members.setdefault(key, []).append(source.id)
        component: set[str] = set()
        pending = [source_id]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            for key in aliases.get(current, ()):
                pending.extend(members.pop(key, ()))
        return component

    async def clear_quota_history(self, source_ids: set[str]) -> None:
        await self._session.execute(
            delete(ModelSourceQuotaObservation).where(ModelSourceQuotaObservation.source_id.in_(source_ids))
        )

    async def usage_summary(
        self, source_id: str, *, window_start: datetime, window_end: datetime
    ) -> ModelSourceUsageResponse:
        output = func.coalesce(RequestLog.output_tokens, RequestLog.reasoning_tokens)
        row = (await self._session.execute(
            select(
                func.count().label("request_count"),
                func.sum(case((RequestLog.status == SUCCESS_STATUS, 1), else_=0)).label("success_count"),
                func.sum(case((RequestLog.status.not_in(NON_ERROR_STATUSES), 1), else_=0)).label("error_count"),
                func.sum(case((RequestLog.status == CANCELLED_STATUS, 1), else_=0)).label("cancelled_count"),
                func.sum(RequestLog.input_tokens).label("input_tokens"),
                func.sum(output).label("output_tokens"),
                func.sum(RequestLog.cached_input_tokens).label("cached_input_tokens"),
                func.sum(RequestLog.cache_creation_input_tokens).label("cache_creation_input_tokens"),
                func.sum(RequestLog.reasoning_tokens).label("reasoning_tokens"),
                func.sum(RequestLog.cost_usd).label("known_cost_usd"),
                func.count(RequestLog.cost_usd).label("priced_count"),
                func.sum(case((or_(RequestLog.input_tokens.is_(None), output.is_(None)), 1), else_=0))
                .label("unknown_usage_count"),
                func.max(RequestLog.requested_at).label("last_request_at"),
            ).where(
                RequestLog.model_source_id == source_id,
                RequestLog.requested_at >= window_start,
                RequestLog.requested_at <= window_end,
                RequestLog.deleted_at.is_(None),
                RequestLogsRepository._exclude_warmup_clause(),
            )
        )).one()
        count = row.request_count
        unpriced = count - row.priced_count
        known_cost = float(row.known_cost_usd) if row.known_cost_usd is not None else (0.0 if count == 0 else None)
        return ModelSourceUsageResponse(
            source_id=source_id, window_start=window_start, window_end=window_end,
            request_count=count, success_count=row.success_count or 0,
            error_count=row.error_count or 0, cancelled_count=row.cancelled_count or 0,
            input_tokens=row.input_tokens if count else 0,
            output_tokens=row.output_tokens if count else 0,
            cached_input_tokens=row.cached_input_tokens if count else 0,
            cache_creation_input_tokens=row.cache_creation_input_tokens if count else 0,
            reasoning_tokens=row.reasoning_tokens if count else 0,
            cost_usd=known_cost if unpriced == 0 else None, known_cost_usd=known_cost,
            unpriced_request_count=unpriced, unknown_usage_count=row.unknown_usage_count or 0,
            last_request_at=row.last_request_at,
        )

    async def active_operation_counts(self) -> dict[str, int]:
        result = await self._session.execute(
            select(ModelSourceRequestOperation.source_id, func.count())
            .where(ModelSourceRequestOperation.status == "dispatching")
            .where(ModelSourceRequestOperation.lease_expires_at > utcnow())
            .group_by(ModelSourceRequestOperation.source_id)
        )
        return {source_id: count for source_id, count in result.all()}

    async def _find_source(
        self,
        model: str,
        *,
        protocol: SourceProtocol,
        allowed_source_ids: set[str] | None = None,
        require_streaming: bool = False,
        only_disabled: bool = False,
        billing_policy: str = "any",
    ) -> ModelSource | None:
        from app.modules.api_keys.service import ApiKeyData
        from app.modules.model_sources.selection import (
            _record_selection,
            _remember_selected_capacity,
            _selection_rank,
            _selection_tier,
            selected_model_entry,
            source_has_capacity,
            source_is_eligible,
        )

        if billing_policy not in {"any", "subscription_only"}:
            return None
        key = ApiKeyData(
            id="repository-policy",
            name="repository-policy",
            key_prefix="",
            allowed_models=None,
            enforced_model=None,
            enforced_reasoning_effort=None,
            enforced_service_tier=None,
            expires_at=None,
            is_active=True,
            created_at=datetime.min,
            last_used_at=None,
            billing_policy=billing_policy,
            source_assignment_scope_enabled=allowed_source_ids is not None,
            assigned_source_ids=sorted(allowed_source_ids or ()),
        )
        sources = await self.list_sources()
        active_operations = await self.active_operation_counts() if not only_disabled else {}
        snapshot = tuple(sources)
        candidates: list[ModelSource] = []
        for source in sources:
            entry = selected_model_entry(source, model)
            if entry is None or (require_streaming and not entry.supports_streaming):
                continue
            if only_disabled:
                if allowed_source_ids is not None and source.id not in allowed_source_ids:
                    continue
                if billing_policy == "subscription_only" and source.billing_mode not in {"subscription", "coding_plan"}:
                    continue
                capabilities = {
                    "messages": source.supports_messages,
                    "chat_completions": source.supports_chat_completions,
                    "responses": source.supports_responses,
                    "embeddings": source.supports_embeddings,
                    "audio_transcriptions": source.supports_audio_transcriptions,
                }
                if not capabilities[protocol]:
                    continue
                if source.is_enabled and entry.is_enabled:
                    continue
            elif not source_is_eligible(
                source, key, model, protocol=protocol, group_sources=sources
            ) or not source_has_capacity(source, group_sources=sources, active_operation_counts=active_operations):
                continue
            candidates.append(source)
        eligible = tuple(candidates)
        candidates.sort(key=lambda source: (
            _selection_tier(source, eligible, model), _selection_rank(source, model, snapshot)
        ))
        if not candidates:
            return None
        selected = candidates[0]
        if not only_disabled:
            _remember_selected_capacity(selected, sources)
            _record_selection(selected)
        return selected

    async def find_chat_source_for_model(
        self,
        model: str,
        *,
        allowed_source_ids: set[str] | None = None,
        require_streaming: bool = False,
        only_disabled: bool = False,
        billing_policy: str = "any",
    ) -> ModelSource | None:
        return await self._find_source(
            model,
            protocol="chat_completions",
            allowed_source_ids=allowed_source_ids,
            require_streaming=require_streaming,
            only_disabled=only_disabled,
            billing_policy=billing_policy,
        )

    async def find_responses_source_for_model(
        self,
        model: str,
        *,
        allowed_source_ids: set[str] | None = None,
        require_streaming: bool = False,
        only_disabled: bool = False,
        billing_policy: str = "any",
    ) -> ModelSource | None:
        return await self._find_source(
            model,
            protocol="responses",
            allowed_source_ids=allowed_source_ids,
            require_streaming=require_streaming,
            only_disabled=only_disabled,
            billing_policy=billing_policy,
        )

    async def find_messages_source_for_model(
        self,
        model: str,
        *,
        allowed_source_ids: set[str] | None = None,
        require_streaming: bool = False,
        billing_policy: str = "any",
    ) -> ModelSource | None:
        return await self._find_source(
            model,
            protocol="messages",
            allowed_source_ids=allowed_source_ids,
            require_streaming=require_streaming,
            billing_policy=billing_policy,
        )

    async def find_audio_transcriptions_source_for_model(
        self,
        model: str,
        *,
        allowed_source_ids: set[str] | None = None,
        billing_policy: str = "any",
    ) -> ModelSource | None:
        return await self._find_source(
            model, protocol="audio_transcriptions", allowed_source_ids=allowed_source_ids, billing_policy=billing_policy
        )

    async def find_embeddings_source_for_model(
        self,
        model: str,
        *,
        allowed_source_ids: set[str] | None = None,
        billing_policy: str = "any",
    ) -> ModelSource | None:
        return await self._find_source(
            model, protocol="embeddings", allowed_source_ids=allowed_source_ids, billing_policy=billing_policy
        )

    async def create(self, row: ModelSource, *, commit: bool = True) -> ModelSource:
        self._session.add(row)
        if commit:
            await self._session.commit()
            await self._session.refresh(row, attribute_names=["models", "oauth_credential", "quota_windows"])
        return row

    async def delete(self, source_id: str) -> bool:
        # Write first: unlike SELECT FOR UPDATE this also serializes claims on
        # SQLite. Runtime operation claims take this same source-row write lock.
        await self._session.execute(
            update(ModelSource).where(ModelSource.id == source_id).values(updated_at=ModelSource.updated_at)
        )
        row = await self.get_by_id(source_id)
        if row is None:
            await self._session.rollback()
            return False
        active = await self._session.scalar(
            select(ModelSourceRequestOperation.id)
            .where(
                ModelSourceRequestOperation.source_id == source_id,
                ModelSourceRequestOperation.status.in_(("dispatching", "unknown")),
            )
            .limit(1)
        )
        if active is not None:
            await self._session.rollback()
            raise ModelSourceHasActiveOperationsError("Source has active or uncertain requests")
        await self._session.delete(row)
        await self._session.commit()
        return True

    async def replace_models(self, source: ModelSource, models: list[ModelSourceModel], *, commit: bool = True) -> None:
        await self._session.execute(delete(ModelSourceModel).where(ModelSourceModel.source_id == source.id))
        for model in models:
            model.source_id = source.id
            self._session.add(model)
        if commit:
            await self._session.commit()
            await self.refresh_models(source)

    async def refresh_models(self, source: ModelSource) -> None:
        await self._session.refresh(source, attribute_names=["models"])

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
