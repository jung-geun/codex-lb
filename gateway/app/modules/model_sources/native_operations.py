"""Durable native uncertainty fences and opaque continuation ownership.

Only active/unknown fingerprints are unique. Terminal operations remain audit
history, never a permanent deduplication key for later valid requests.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import utcnow
from app.db.models import ModelSource, ModelSourceContinuationOwner, ModelSourceQuotaWindow, ModelSourceRequestOperation
from app.db.session import sqlite_writer_section
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.selection import selected_model_entry, source_is_eligible, sources_share_capacity
from app.modules.model_sources.subscription_runtime import _snapshot_guard

if TYPE_CHECKING:
    from app.modules.model_sources.anthropic import NativeQuotaObservation

NATIVE_OPERATION_OWNER = uuid4().hex
NATIVE_LEASE_SECONDS = 90
NATIVE_HEARTBEAT_SECONDS = 10


class NativeOperationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_uncertain_operation(
        self, scope: str, fingerprint: str, *, legacy_fingerprints: Collection[str] = ()
    ) -> bool:
        """Fence replay before source selection; claim_operation rechecks atomically."""
        return await self._session.scalar(
            select(ModelSourceRequestOperation.id).where(
                ModelSourceRequestOperation.api_key_scope == scope,
                ModelSourceRequestOperation.request_fingerprint.in_({fingerprint, *legacy_fingerprints}),
                ModelSourceRequestOperation.status.in_(("dispatching", "unknown")),
            ).limit(1)
        ) is not None

    async def claim_operation(
        self,
        scope: str,
        fingerprint: str,
        source_id: str,
        model: str,
        *,
        api_key: ApiKeyData | None = None,
        require_streaming: bool = False,
        expected_source: ModelSource | None = None,
        legacy_fingerprints: Collection[str] = (),
    ) -> str | None:
        async with sqlite_writer_section():
            # SQLite needs a write before any reads to avoid read->write snapshot
            # upgrades. PostgreSQL locks sources in one stable order, including
            # all shared-principal/group members, before capacity is rechecked.
            if self._session.get_bind().dialect.name == "sqlite":
                await self._session.execute(
                    update(ModelSource).where(ModelSource.id == source_id).values(updated_at=ModelSource.updated_at)
                )
            else:
                await self._session.execute(select(ModelSource.id).order_by(ModelSource.id).with_for_update())
            sources = await ModelSourcesRepository(self._session).list_sources()
            source = next((item for item in sources if item.id == source_id), None)
            peers = [peer for peer in sources if source is not None and sources_share_capacity(source, peer)]
            now = utcnow()
            await self._session.execute(
                update(ModelSourceRequestOperation)
                .where(
                    ModelSourceRequestOperation.status == "dispatching",
                    ModelSourceRequestOperation.source_id.in_([peer.id for peer in peers] or [source_id]),
                    or_(
                        ModelSourceRequestOperation.lease_expires_at.is_(None),
                        ModelSourceRequestOperation.lease_expires_at <= now,
                    ),
                )
                .values(status="unknown")
            )
            existing = await self._session.scalar(
                select(ModelSourceRequestOperation.id).where(
                    ModelSourceRequestOperation.api_key_scope == scope,
                    ModelSourceRequestOperation.request_fingerprint.in_({fingerprint, *legacy_fingerprints}),
                    ModelSourceRequestOperation.status.in_(("dispatching", "unknown")),
                )
            )
            if existing is not None:
                await self._session.commit()
                return None
            if source is None or not source_is_eligible(
                source, api_key, model, protocol="messages", group_sources=sources
            ):
                await self._session.rollback()
                raise ProviderError("Selected native source is no longer eligible")
            if expected_source is not None and source.updated_at != expected_source.updated_at:
                await self._session.rollback()
                raise ProviderError("Selected native source changed before dispatch")
            entry = selected_model_entry(source, model)
            if expected_source is not None:
                expected_entry = selected_model_entry(expected_source, model)
                if entry is None or expected_entry is None or (
                    entry.model, entry.upstream_model or entry.model
                ) != (expected_entry.model, expected_entry.upstream_model or expected_entry.model):
                    await self._session.rollback()
                    raise ProviderError("Selected native model mapping changed before dispatch")
            if require_streaming and (entry is None or not entry.supports_streaming):
                await self._session.rollback()
                raise ProviderError("Selected native source no longer supports streaming")
            counts = dict(
                (
                    await self._session.execute(
                        select(
                            ModelSourceRequestOperation.source_id,
                            func.count(),
                        )
                        .where(
                            ModelSourceRequestOperation.source_id.in_([peer.id for peer in peers]),
                            ModelSourceRequestOperation.status == "dispatching",
                        )
                        .group_by(ModelSourceRequestOperation.source_id)
                    )
                ).all()
            )
            limits = [peer.max_concurrency for peer in peers if peer.max_concurrency is not None]
            group_limit = (
                min(limits) if limits else (1 if source.billing_mode in {"subscription", "coding_plan"} else None)
            )
            if (source.max_concurrency is not None and counts.get(source.id, 0) >= source.max_concurrency) or (
                group_limit is not None and sum(counts.values()) >= group_limit
            ):
                await self._session.rollback()
                raise ProviderError("Selected native source is busy")
            operation_id = uuid4().hex
            self._session.add(
                ModelSourceRequestOperation(
                    id=operation_id,
                    api_key_scope=scope,
                    request_fingerprint=fingerprint,
                    source_id=source_id,
                    model=model,
                    status="dispatching",
                    owner_uuid=NATIVE_OPERATION_OWNER,
                    lease_expires_at=now + timedelta(seconds=NATIVE_LEASE_SECONDS),
                )
            )
            try:
                await self._session.commit()
            except IntegrityError:
                await self._session.rollback()
                existing = await self._session.scalar(
                    select(ModelSourceRequestOperation.id).where(
                        ModelSourceRequestOperation.api_key_scope == scope,
                        ModelSourceRequestOperation.request_fingerprint.in_({fingerprint, *legacy_fingerprints}),
                        ModelSourceRequestOperation.status.in_(("dispatching", "unknown")),
                    )
                )
                if existing is None:
                    raise
                return None
            return operation_id

    async def finish_operation(
        self,
        operation_id: str,
        status: Literal["completed", "rejected", "unknown"],
    ) -> None:
        async with sqlite_writer_section():
            await self._session.execute(
                update(ModelSourceRequestOperation)
                .where(
                    ModelSourceRequestOperation.id == operation_id,
                    ModelSourceRequestOperation.owner_uuid == NATIVE_OPERATION_OWNER,
                    ModelSourceRequestOperation.status.in_(("dispatching", "unknown")),
                )
                .values(status=status, lease_expires_at=None)
            )
            await self._session.commit()

    async def heartbeat_operation(self, operation_id: str) -> bool:
        async with sqlite_writer_section():
            now = utcnow()
            result = await self._session.execute(
                update(ModelSourceRequestOperation)
                .where(
                    ModelSourceRequestOperation.id == operation_id,
                    ModelSourceRequestOperation.owner_uuid == NATIVE_OPERATION_OWNER,
                    ModelSourceRequestOperation.status == "dispatching",
                    ModelSourceRequestOperation.lease_expires_at > now,
                )
                .values(lease_expires_at=now + timedelta(seconds=NATIVE_LEASE_SECONDS))
            )
            await self._session.commit()
            return result.rowcount == 1

    async def resolve_continuation_owners(
        self,
        scope: str,
        token_hashes: set[str] | frozenset[str],
    ) -> list[tuple[str, str]] | None:
        if not token_hashes:
            return []
        rows = (
            await self._session.execute(
                select(
                    ModelSourceContinuationOwner.token_hash,
                    ModelSourceContinuationOwner.source_id,
                    ModelSourceContinuationOwner.principal,
                ).where(
                    ModelSourceContinuationOwner.api_key_scope == scope,
                    ModelSourceContinuationOwner.token_hash.in_(token_hashes),
                )
            )
        ).all()
        if len(rows) != len(token_hashes):
            return None
        return [(row.source_id, row.principal) for row in rows]

    async def record_continuation_owners(
        self,
        scope: str,
        source_id: str,
        principal: str,
        token_hashes: set[str] | frozenset[str],
    ) -> bool:
        async with sqlite_writer_section():
            return await self._record_continuation_owners(scope, source_id, principal, token_hashes)

    async def _record_continuation_owners(
        self,
        scope: str,
        source_id: str,
        principal: str,
        token_hashes: set[str] | frozenset[str],
    ) -> bool:
        if not token_hashes:
            return True
        dialect = self._session.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise RuntimeError("Native continuation ownership requires SQLite or PostgreSQL")
        # A collision must never overwrite an established principal, even if
        # two neutral initial requests with one session identity race.
        ordered_hashes = sorted(token_hashes)
        for offset in range(0, len(token_hashes), 256):
            hashes = ordered_hashes[offset : offset + 256]
            await self._session.execute(
                insert(ModelSourceContinuationOwner)
                .values(
                    [
                        {
                            "api_key_scope": scope,
                            "token_hash": token_hash,
                            "source_id": source_id,
                            "principal": principal,
                        }
                        for token_hash in hashes
                    ]
                )
                .on_conflict_do_nothing(index_elements=["api_key_scope", "token_hash"])
            )
        owners = await self.resolve_continuation_owners(scope, token_hashes)
        if owners is None or any(owner != (source_id, principal) for owner in owners):
            await self._session.rollback()
            return False
        await self._session.commit()
        return True

    async def record_native_quota(
        self,
        source: ModelSource,
        observations: list[NativeQuotaObservation],
        *,
        observed_at: datetime,
    ) -> None:
        if source.provider != "claude" or source.auth_type != "claude_oauth" or not observations:
            return
        async with sqlite_writer_section():
            guarded = await self._session.execute(
                update(ModelSource).where(*_snapshot_guard(source)).values(updated_at=ModelSource.updated_at)
            )
            if guarded.rowcount != 1:
                await self._session.rollback()
                return
            for observation in observations:
                if observation.resets_at <= observed_at:
                    continue
                row = await self._session.get(ModelSourceQuotaWindow, (source.id, observation.window_key))
                if row is not None:
                    if row.observed_at > observed_at or (row.resets_at and row.resets_at > observation.resets_at):
                        continue
                    ratio = observation.used_ratio
                    if row.resets_at == observation.resets_at and row.used_ratio is not None:
                        ratio = max(ratio, row.used_ratio)
                    row.used_ratio, row.resets_at, row.observed_at = ratio, observation.resets_at, observed_at
                else:
                    self._session.add(
                        ModelSourceQuotaWindow(
                            source_id=source.id,
                            window_key=observation.window_key,
                            model=observation.model,
                            used_ratio=observation.used_ratio,
                            resets_at=observation.resets_at,
                            observed_at=observed_at,
                        )
                    )
            # Partial response headers do not refresh full-account quota or
            # entitlement freshness, nor erase unreported model windows.
            await self._session.commit()
