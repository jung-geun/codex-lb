"""Pinned Claude five-hour warmups with durable, non-retryable dispatch claims.

A completed message is not evidence that a quota clock started. Only a later
provider observation can promote success to window_started. An idle window with
no reset has one permanent identity until the provider supplies a real reset;
we never manufacture a rolling five-hour clock from a local timestamp.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update

from app.core.audit.service import AuditService, AuditTarget
from app.core.utils.shared_future import _await_cleanup_deferring_cancellation
from app.db.models import (
    ModelSource,
    ModelSourceLimitWarmup,
    ModelSourceModel,
    ModelSourceQuotaWindow,
    ModelSourceRequestOperation,
)
from app.db.session import detach_session_objects, get_background_session, sqlite_writer_section
from app.modules.model_sources import anthropic, subscription_runtime
from app.modules.model_sources.catalog import source_model_cost_usd
from app.modules.model_sources.native_operations import NATIVE_LEASE_SECONDS, NATIVE_OPERATION_OWNER
from app.modules.model_sources.providers.base import ProviderError, utcnow
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.schemas import ModelSourceResponse
from app.modules.model_sources.selection import (
    QUOTA_MAX_AGE,
    _effective_quota_windows,
    _idle_five_hour,
    _quota_usable,
    _utc,
    _window_exhausted,
    source_capacity_group,
    source_has_capacity,
    source_is_eligible,
    sources_share_capacity,
)
from app.modules.request_logs.repository import RequestLogsRepository

WARMUP_TIMEOUT_SECONDS = 30
WARMUP_COOLDOWN = timedelta(hours=5)


def warmup_supported(source: ModelSource) -> bool:
    return (
        source.provider == "claude"
        and source.auth_type == "claude_oauth"
        and source.billing_mode == "subscription"
        and source.base_url == "https://api.anthropic.com"
        and source.messages_base_url in {None, "https://api.anthropic.com", "https://api.anthropic.com/v1"}
        and source.supports_messages
    )


def _principal(source: ModelSource) -> str:
    return hashlib.sha256(source_capacity_group(source).encode()).hexdigest()


def _five_hour(source: ModelSource, sources: Sequence[ModelSource]) -> ModelSourceQuotaWindow | None:
    windows = _effective_quota_windows(source, "", sources)
    return next((window for window in windows if window.window_key == "five_hour" and window.model is None), None)


def _window_identity(
    source: ModelSource,
    sources: Sequence[ModelSource],
    attempts: Sequence[ModelSourceLimitWarmup],
) -> str:
    window = _five_hour(source, sources)
    if window is not None and window.resets_at is not None:
        return "reset:" + _utc(window.resets_at).isoformat()
    # Claude can clear resets_at when a completed window becomes idle. Retain
    # its real provider-observed boundary rather than reuse the initial idle key.
    completed_resets = [
        _utc(attempt.reset_at)
        for attempt in attempts
        if attempt.status != "skipped"
        and attempt.reset_at is not None
        and _utc(attempt.reset_at) > _utc(attempt.attempted_at)
        and _utc(attempt.reset_at) <= _utc(utcnow())
    ]
    return "reset:" + max(completed_resets).isoformat() if completed_resets else "idle:no-observed-reset"


def _models(source: ModelSource) -> list[ModelSourceModel]:
    # Native discovery/entitlement, never a hardcoded model that might be paid.
    return sorted(
        (
            entry
            for entry in source.models
            if entry.is_enabled and entry.subscription_eligible and not entry.subscription_restricted
        ),
        key=lambda entry: (
            0
            if "haiku" in (entry.upstream_model or entry.model).lower()
            else 1
            if "sonnet" in (entry.upstream_model or entry.model).lower()
            else 2,
            entry.model,
        ),
    )


def _eligible_model(source: ModelSource, sources: Sequence[ModelSource]) -> ModelSourceModel | None:
    return next(
        (
            entry
            for entry in _models(source)
            if source_is_eligible(
                source,
                None,
                entry.model,
                protocol="messages",
                group_sources=sources,
                allow_idle_five_hour=True,
            )
        ),
        None,
    )


def _prior_attempt_reason(
    source: ModelSource,
    sources: Sequence[ModelSource],
    attempts: Sequence[ModelSourceLimitWarmup],
) -> str | None:
    now = utcnow()
    window = _five_hour(source, sources)
    for attempt in attempts:
        if attempt.status == "skipped":
            continue
        # A persisted native reset remains evidence when Claude clears the
        # current reset on becoming idle. Local elapsed time alone is not.
        resets = [
            _utc(reset)
            for reset in (window.resets_at if window is not None else None, attempt.reset_at)
            if reset is not None and _utc(reset) > _utc(attempt.attempted_at)
        ]
        reset_observed = (
            window is not None and bool(resets) and max(resets) <= _utc(now) and _utc(window.observed_at) >= max(resets)
        )
        if not reset_observed or _utc(now) - _utc(attempt.attempted_at) < WARMUP_COOLDOWN:
            if attempt.status == "unknown" or (
                attempt.status == "dispatching"
                and _utc(now) - _utc(attempt.attempted_at) > timedelta(seconds=NATIVE_LEASE_SECONDS)
            ):
                return "acceptance_unknown"
            return "already_attempted" if attempt.status != "dispatching" else "in_progress"
    return None


def warmup_unavailable_reason(source: ModelSource, sources: Sequence[ModelSource] = ()) -> str | None:
    if not warmup_supported(source):
        return "unsupported"
    if not source.is_enabled:
        return "disabled"
    if not source.limit_warmup_enabled:
        return "warmup_disabled"
    now = utcnow()
    credential = source.oauth_credential
    if credential is None or not credential.account_uuid or not credential.organization_uuid:
        return "principal_unverified"
    if source.entitlement_status != "verified" or source.entitlement_verified_at is None:
        return "entitlement_unverified"
    age = _utc(now) - _utc(source.entitlement_verified_at)
    if age < timedelta(seconds=-30) or age > QUOTA_MAX_AGE:
        return "entitlement_stale"
    if source.extra_usage_enabled is not False:
        return "extra_usage_not_disabled"
    peers = [peer for peer in (source, *sources) if sources_share_capacity(source, peer)]
    if any(peer.cooldown_until is not None and _utc(peer.cooldown_until) > _utc(now) for peer in peers):
        return "cooldown"
    if source.quota_error or source.quota_checked_at is None:
        return "quota_unavailable"
    age = _utc(now) - _utc(source.quota_checked_at)
    if age < timedelta(seconds=-30) or age > QUOTA_MAX_AGE:
        return "quota_stale"
    shared = _effective_quota_windows(source, "", sources)
    weekly = [window for window in shared if window.window_key == "seven_day"]
    if not weekly:
        return "weekly_quota_unavailable"
    if any(_window_exhausted(window) for window in weekly):
        return "weekly_exhausted"
    if any(not _quota_usable(window, _utc(now), allow_exhausted=False) for window in weekly):
        return "weekly_quota_unavailable"
    window = _five_hour(source, sources)
    if window is None:
        return "five_hour_unavailable"
    if window.resets_at is not None and _utc(window.resets_at) > _utc(now):
        return "five_hour_active"
    if not _idle_five_hour(window, _utc(now)):
        return "five_hour_not_idle"
    if not _models(source):
        return "model_entitlement_unavailable"
    if _eligible_model(source, sources) is None:
        return "model_quota_unavailable"
    principals = {_principal(peer) for peer in peers}
    attempts = [attempt for peer in peers for attempt in peer.limit_warmups if attempt.principal in principals]
    return _prior_attempt_reason(source, sources, attempts)


async def _claim(source_id: str) -> tuple[ModelSource, str, str] | None:
    async with get_background_session() as session, sqlite_writer_section():
        # Match native admission's lock order. Both request paths serialize on
        # source rows, so the warmup occupies real durable group capacity.
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(
                update(ModelSource).where(ModelSource.id == source_id).values(updated_at=ModelSource.updated_at)
            )
        else:
            await session.execute(select(ModelSource.id).order_by(ModelSource.id).with_for_update())
        sources = await ModelSourcesRepository(session).list_sources()
        source = next((item for item in sources if item.id == source_id), None)
        if source is None:
            raise ProviderError("Model source not found")
        if not warmup_supported(source):
            raise ProviderError("Warmup requires a fixed Claude OAuth subscription source")
        peers = [peer for peer in sources if sources_share_capacity(source, peer)]
        peer_ids = [peer.id for peer in peers]
        principals = [_principal(peer) for peer in peers]
        now = utcnow()
        await session.execute(
            update(ModelSourceRequestOperation)
            .where(
                ModelSourceRequestOperation.source_id.in_(peer_ids),
                ModelSourceRequestOperation.status == "dispatching",
                or_(
                    ModelSourceRequestOperation.lease_expires_at.is_(None),
                    ModelSourceRequestOperation.lease_expires_at <= now,
                ),
            )
            .values(status="unknown")
        )
        attempts = list(
            (
                await session.scalars(
                    select(ModelSourceLimitWarmup).where(
                        ModelSourceLimitWarmup.principal.in_(principals),
                    )
                )
            ).all()
        )
        for attempt in attempts:
            if attempt.status == "dispatching" and _utc(now) - _utc(attempt.attempted_at) > timedelta(
                seconds=NATIVE_LEASE_SECONDS
            ):
                attempt.status = "unknown"
                attempt.error_message = "acceptance_unknown"
        reason = warmup_unavailable_reason(source, sources)
        if reason is None:
            reason = _prior_attempt_reason(source, sources, attempts)
        active = list(
            (
                await session.scalars(
                    select(ModelSourceRequestOperation.source_id).where(
                        ModelSourceRequestOperation.source_id.in_(peer_ids),
                        ModelSourceRequestOperation.status == "dispatching",
                    )
                )
            ).all()
        )
        if reason is None and not source_has_capacity(
            source,
            group_sources=sources,
            active_operation_counts=dict(Counter(active)),
        ):
            reason = "capacity_busy"
        identity = _window_identity(source, sources, attempts)
        existing = next(
            (
                attempt
                for attempt in attempts
                if attempt.principal == _principal(source) and attempt.window_identity == identity
            ),
            None,
        )
        if reason is not None:
            # Skips never erase an accepted/ambiguous operation's history.
            if existing is None:
                session.add(
                    ModelSourceLimitWarmup(
                        id=uuid4().hex,
                        source_id=source.id,
                        principal=_principal(source),
                        window_key="five_hour",
                        window_identity=identity,
                        status="skipped",
                        attempted_at=now,
                        completed_at=now,
                        error_message=reason,
                    )
                )
            elif existing.status == "skipped":
                existing.attempted_at, existing.completed_at, existing.error_message = now, now, reason
            await session.commit()
            return None
        if existing is not None and existing.status != "skipped":
            await session.commit()
            return None
        entry = _eligible_model(source, sources)
        if entry is None:
            raise ProviderError("Claude warmup model is no longer eligible")
        operation_id = existing.id if existing is not None else uuid4().hex
        window = _five_hour(source, sources)
        values = dict(
            source_id=source.id,
            principal=_principal(source),
            window_key="five_hour",
            window_identity=identity,
            status="dispatching",
            attempted_at=now,
            completed_at=None,
            error_message=None,
            reset_at=window.resets_at if window is not None else None,
            model=entry.model,
        )
        if existing is None:
            session.add(ModelSourceLimitWarmup(id=operation_id, **values))
        else:
            for name, value in values.items():
                setattr(existing, name, value)
        session.add(
            ModelSourceRequestOperation(
                id=operation_id,
                api_key_scope="source_limit_warmup",
                request_fingerprint=_principal(source) + ":" + identity,
                source_id=source.id,
                model=entry.model,
                status="dispatching",
                owner_uuid=NATIVE_OPERATION_OWNER,
                lease_expires_at=now + timedelta(seconds=NATIVE_LEASE_SECONDS),
            )
        )
        await session.commit()
        detach_session_objects(session)
        return source, operation_id, entry.model


async def _finish(
    source: ModelSource,
    operation_id: str,
    model: str,
    status: str,
    error: str | None,
    response: anthropic.NativeResponse | None,
    started: float,
) -> None:
    usage = response.usage if response is not None else None
    cost = (
        source_model_cost_usd(
            source,
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_creation_5m_input_tokens=usage.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=usage.cache_creation_1h_input_tokens,
            served_model=usage.served_model,
        )
        if usage is not None
        else None
    )
    async with get_background_session() as session, sqlite_writer_section():
        await session.execute(
            update(ModelSourceRequestOperation)
            .where(
                ModelSourceRequestOperation.id == operation_id,
                ModelSourceRequestOperation.owner_uuid == NATIVE_OPERATION_OWNER,
            )
            .values(
                status="completed" if status == "success" else "rejected" if status == "rejected" else "unknown",
                lease_expires_at=None,
            )
        )
        await session.execute(
            update(ModelSourceLimitWarmup)
            .where(ModelSourceLimitWarmup.id == operation_id)
            .values(
                status=status,
                completed_at=utcnow(),
                error_message=error,
            )
        )
        await session.commit()
    async with get_background_session() as session:
        await RequestLogsRepository(session).add_log(
            account_id=None,
            request_id="source-warmup-" + operation_id,
            model=model,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cached_input_tokens=usage.cached_input_tokens if usage else None,
            cache_creation_input_tokens=usage.cache_creation_input_tokens if usage else None,
            cache_creation_5m_input_tokens=usage.cache_creation_5m_input_tokens if usage else None,
            cache_creation_1h_input_tokens=usage.cache_creation_1h_input_tokens if usage else None,
            latency_ms=int((time.monotonic() - started) * 1000),
            status="success" if status == "success" else "error",
            error_code=error,
            error_message=error,
            request_kind="limit_warmup",
            source="limit_warmup",
            model_source_id=source.id,
            model_source_kind=source.kind,
            provider="claude",
            billing_mode="subscription",
            cost_usd=cost,
            cost_kind="api_equivalent",
            served_model=usage.served_model if usage else None,
            upstream_status_code=response.status if response else None,
        )
    AuditService.log_async(
        "model_source_warmup_" + status,
        target=AuditTarget("model_source", source.id),
        details={
            "source_id": source.id,
            "operation_id": operation_id,
            "status": status,
            "reason": error,
        },
    )


async def warm_source(source_id: str, *, manual: bool = True) -> ModelSourceResponse:
    # Manual requests re-observe provider quota before admission. Automatic
    # requests already follow the leader's quota refresh cycle. Neither path
    # bypasses opt-in, durable claims, or any dispatch eligibility fence.
    if manual:
        source = await subscription_runtime.load_source(source_id)
        if warmup_supported(source) and source.is_enabled and source.limit_warmup_enabled:
            await subscription_runtime.refresh_source_quota(source_id)
    claimed = await _claim(source_id)
    if claimed is None:
        from app.modules.model_sources.service import _to_response

        return _to_response(await subscription_runtime.load_source(source_id))
    source, operation_id, model = claimed
    entry = next(entry for entry in source.models if entry.model == model)
    started = time.monotonic()
    response = None
    status, error = "unknown", "acceptance_unknown"
    AuditService.log_async(
        "model_source_warmup_dispatched",
        target=AuditTarget("model_source", source.id),
        details={
            "source_id": source.id,
            "operation_id": operation_id,
            "trigger": "manual" if manual else "automatic",
        },
    )
    try:
        async with asyncio.timeout(WARMUP_TIMEOUT_SECONDS):
            native_result = await anthropic.open_native(
                source,
                {
                    "model": entry.upstream_model or model,
                    "messages": [{"role": "user", "content": "Say OK."}],
                    "max_tokens": 1,
                    "stream": False,
                },
                incoming_headers={},
                expected_principal=anthropic.native_source_principal(source),
            )
            if not isinstance(native_result, anthropic.NativeResponse):
                raise TypeError("Claude warmup returned an unexpected stream")
            response = native_result
            status, error = "success", None
    except anthropic.NativeRejected as exc:
        response = exc.response
        status, error = ("rejected", "provider_rejected") if exc.unaccepted else ("unknown", "acceptance_unknown")
        await subscription_runtime.on_source_rejection(source, exc.response.status, exc.response.headers)
    except ProviderError:
        # Credential/configuration rejection is raised before native dispatch.
        status, error = "rejected", "admission_changed"
    except asyncio.CancelledError:
        await _await_cleanup_deferring_cancellation(
            _finish(source, operation_id, model, "unknown", "acceptance_unknown", None, started)
        )
        raise
    except Exception:
        # Timeout, malformed completion and disconnect can all mean acceptance.
        # Never retry, choose another source, or expose exception/token details.
        status, error = "unknown", "acceptance_unknown"
    cancellation = await _await_cleanup_deferring_cancellation(
        _finish(source, operation_id, model, status, error, response, started)
    )
    if cancellation is not None:
        raise cancellation
    return await subscription_runtime.refresh_source_quota(source_id)


async def observe_warmup_window(source: ModelSource) -> None:
    """Retain an actual provider reset, including after an ambiguous dispatch.

    Observing a window never turns unknown acceptance into message success.
    This hook also runs on later scheduler refreshes after a process restart.
    """
    if not warmup_supported(source) or source.quota_error is not None:
        return
    window = _five_hour(source, ())
    if window is None or window.resets_at is None or _utc(window.resets_at) <= _utc(utcnow()):
        return
    async with get_background_session() as session, sqlite_writer_section():
        attempts = list(
            (
                await session.scalars(
                    select(ModelSourceLimitWarmup).where(
                        ModelSourceLimitWarmup.principal == _principal(source),
                        ModelSourceLimitWarmup.status.in_(("dispatching", "unknown", "success", "window_started")),
                        ModelSourceLimitWarmup.attempted_at <= window.observed_at,
                    )
                )
            ).all()
        )
        for attempt in attempts:
            if attempt.reset_at is None or _utc(attempt.reset_at) <= _utc(attempt.attempted_at):
                attempt.reset_at = window.resets_at
            if attempt.status == "success":
                attempt.status = "window_started"
        await session.commit()


async def warm_subscription_sources() -> None:
    async with get_background_session() as session:
        source_ids = list(
            (
                await session.scalars(
                    select(ModelSource.id).where(
                        ModelSource.is_enabled.is_(True),
                        ModelSource.limit_warmup_enabled.is_(True),
                        ModelSource.provider == "claude",
                        ModelSource.auth_type == "claude_oauth",
                    )
                )
            ).all()
        )
    for source_id in source_ids:
        try:
            await warm_source(source_id, manual=False)
        except ProviderError:
            # Concurrent deletion/configuration changes affect only this source.
            continue
