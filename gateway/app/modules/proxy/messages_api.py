"""Anthropic-native routes with the existing source admission and settlement owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core.auth.dependencies import validate_gateway_api_key
from app.core.exceptions import AppError
from app.core.types import JsonValue
from app.core.utils.shared_future import _await_cleanup_deferring_cancellation, _await_task_deferring_cancellation
from app.db.session import get_background_session
from app.modules.api_keys.service import ApiKeyRequestUsageBudget
from app.modules.model_sources.anthropic import (
    NativeRejected,
    NativeResponse,
    NativeStream,
    NativeStreamIncomplete,
    continuation_token,
    native_continuation_tokens,
    native_quota_observations,
    native_source_principal,
    open_native,
)
from app.modules.model_sources.forwarding import SourceResponsesStream, SourceTimings, SourceUsage
from app.modules.model_sources.native_operations import (
    NATIVE_HEARTBEAT_SECONDS,
    NATIVE_LEASE_SECONDS,
    NativeOperationsRepository,
)
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.selection import (
    _model_namespace,
    effective_model_for_api_key,
    select_model_source,
    selected_model_entry,
)
from app.modules.model_sources.subscription_runtime import on_source_rejection
from app.modules.proxy.api import (
    _enforce_request_limits,
    _release_reservation,
    _settle_source_reservation,
    _source_cost_is_unavailable,
)
from app.modules.proxy.source_admission import try_claim
from app.modules.proxy.source_dispatch import (
    ClientDisconnectedDuringOpen,
    SourceDispatch,
    SourceStreamingResponse,
    open_with_disconnect_watch,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/messages", tags=["messages"])
MAX_UNACCEPTED_ATTEMPTS = 3


def native_error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"type": "error", "error": {"type": kind, "message": message}})


class NativeOwnershipError(AppError):
    status_code = 403
    message = "Native continuation ownership is unknown, changed, or unavailable"


@dataclass(slots=True)
class NativeDispatch(SourceDispatch):
    operation_id: str | None = None
    operation_outcome: Literal["completed", "rejected", "unknown"] = "unknown"
    _operation_done: bool = field(default=False, init=False)
    _persisted_tokens: set[str] = field(default_factory=set, init=False)
    response_headers: dict[str, str] = field(default_factory=dict)
    headers_observed_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    _headers_recorded: bool = field(default=False, init=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False)
    lease_lost: bool = field(default=False, init=False)

    def start_heartbeat(self) -> None:
        self._heartbeat_task = self.scheduler.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        operation_id = self.operation_id
        if operation_id is None:
            raise RuntimeError("Native heartbeat requires a durable operation")
        last_success = self.clock.monotonic()
        while True:
            await self.scheduler.sleep(NATIVE_HEARTBEAT_SECONDS)
            try:
                async with get_background_session() as session:
                    alive = await NativeOperationsRepository(session).heartbeat_operation(operation_id)
                if not alive:
                    self.lease_lost = True
                    if self.stream is not None:
                        await self.close_source()
                    return
                last_success = self.clock.monotonic()
            except Exception:
                logger.warning("native_operation_heartbeat_failed operation_id=%s", self.operation_id, exc_info=True)
                if self.clock.monotonic() - last_success >= NATIVE_LEASE_SECONDS:
                    self.lease_lost = True
                    if self.stream is not None:
                        await self.close_source()
                    return

    async def stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None:
            task.cancel()
            try:
                await _await_task_deferring_cancellation(task)
            except asyncio.CancelledError:
                pass

    async def register_continuations(self, tokens: set[str] | frozenset[str]) -> None:
        pending = set(tokens) - self._persisted_tokens
        if not pending:
            return
        if len(self._persisted_tokens) + len(pending) > 4096:
            raise NativeOwnershipError("Too many native continuation identities")
        async with get_background_session() as session:
            recorded = await NativeOperationsRepository(session).record_continuation_owners(
                self.api_key.id if self.api_key else "anonymous",
                self.source.id,
                native_source_principal(self.source),
                pending,
            )
        if not recorded:
            raise NativeOwnershipError()
        self._persisted_tokens.update(pending)

    async def finish(
        self,
        *,
        status: Literal["success", "error", "cancelled"],
        error_code: str | None = None,
        error_message: str | None = None,
        usage: SourceUsage | None = None,
        upstream_status_code: int | None = None,
        timings: SourceTimings | None = None,
    ) -> None:
        if self.finished:
            return
        self.request_completed_at = self.clock.now().replace(tzinfo=None)
        if timings is None and (self.usage_holder is None or self.usage_holder.timings is None) and self.sent_at:
            # Native Messages has actual usage but no compatible-server timing
            # metrics. Preserve measured execution timing for quota matching;
            # missing TTFT is not zero and historical durations are not guessed.
            timings = SourceTimings(
                latency_first_token_ms=None,
                latency_ms=max(0, round((self.clock.monotonic() - self.sent_at) * 1000)),
            )
        await _await_cleanup_deferring_cancellation(self.stop_heartbeat())
        if not self._operation_done and self.operation_id is not None:
            operation_id = self.operation_id
            self._operation_done = True

            async def persist_outcome() -> None:
                async with get_background_session() as session:
                    await NativeOperationsRepository(session).finish_operation(operation_id, self.operation_outcome)

            try:
                await _await_cleanup_deferring_cancellation(persist_outcome())
            except Exception:
                # A failed terminal write retains the durable dispatching fence.
                logger.exception("native_operation_outcome_write_failed operation_id=%s", operation_id)
        await SourceDispatch.finish(
            self,
            status=status,
            error_code=error_code,
            error_message=error_message,
            usage=usage,
            upstream_status_code=upstream_status_code,
            timings=timings,
        )
        if not self._headers_recorded:
            self._headers_recorded = True
            upstream_model = next(
                (entry.upstream_model or entry.model for entry in self.source.models if entry.model == self.model),
                self.model,
            )
            observations = native_quota_observations(self.response_headers, model=upstream_model)
            if observations:

                async def persist_headers() -> None:
                    async with get_background_session() as session:
                        await NativeOperationsRepository(session).record_native_quota(
                            self.source,
                            observations,
                            observed_at=self.headers_observed_at,
                        )

                try:
                    await _await_cleanup_deferring_cancellation(persist_headers())
                except Exception:
                    logger.warning("native_quota_header_write_failed source_id=%s", self.source.id, exc_info=True)


async def _native_body(owner: NativeDispatch) -> AsyncIterator[bytes]:
    owner.body_started = True
    stream = owner.stream
    assert stream is not None
    try:
        async for chunk in stream.body:
            await owner.register_continuations(stream.usage_holder.continuation_tokens)
            owner.observe_stream()
            if stream.usage_holder.first_content_seen:
                owner.content_delivered = True
            terminal = stream.usage_holder.terminal_kind
            if terminal in {"completed", "error"}:
                # Completion is proven by this native frame, not a later EOF
                # the client may never wait for after seeing message_stop.
                owner.operation_outcome = "completed"
                await owner.finish(status="success" if terminal == "completed" else "error")
            yield chunk
            if terminal in {"completed", "error"}:
                return
        terminal = stream.usage_holder.terminal_kind
        if terminal in {"completed", "error"}:
            owner.operation_outcome = "completed"
            await owner.finish(status="success" if terminal == "completed" else "error")
        else:
            await owner.finish(
                status="error",
                error_code="native_stream_truncated",
                error_message="Upstream ended without message_stop",
            )
    except asyncio.CancelledError:
        await _await_cleanup_deferring_cancellation(owner.finish(status="cancelled", error_code="client_disconnected"))
        raise
    except NativeStreamIncomplete as exc:
        await owner.finish(status="error", error_code="native_stream_truncated")
        if exc.partial_frame:
            # Abort rather than completing an upstream partial SSE record.
            raise
        yield (
            "event: error\ndata: "
            + json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": (
                            "Upstream stream ended without message_stop; outcome is uncertain. "
                            "Do not replay this request."
                        ),
                    },
                }
            )
            + "\n\n"
        ).encode()
    except (aiohttp.ClientError, TimeoutError):
        # Never append a synthetic blank line: it could dispatch an upstream
        # partial message_stop which the native client had not received.
        await owner.finish(status="error", error_code="native_stream_uncertain")
        raise
    except BaseException:
        await _await_cleanup_deferring_cancellation(owner.finish(status="cancelled", error_code="dispatch_interrupted"))
        raise


def _literal_fingerprint(payload: dict[str, JsonValue]) -> str:
    identity = {key: value for key, value in payload.items() if key != "stream"}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fingerprint(payload: dict[str, JsonValue]) -> str:
    # Normalize only trusted provider/native identities, never operator aliases
    # or the literal request model used by ACL and reservation admission.
    model = payload.get("model")
    if isinstance(model, str):
        family, native = _model_namespace(model)
        if family is not None:
            payload = {**payload, "model": [family, native]}
    # Stream mode and client retry IDs cannot bypass uncertainty. Terminal
    # operations still release the fence for later valid identical requests.
    return _literal_fingerprint(payload)


def _legacy_fingerprints(payload: dict[str, JsonValue]) -> frozenset[str]:
    # Existing durable rows hashed the literal model. Check every equivalent
    # trusted spelling before selection AND under the claim lock, so upgrades
    # do not release an already accepted or uncertain turn.
    model = str(payload["model"])
    aliases = {model}
    family, native = _model_namespace(model)
    if family is not None:
        aliases.add(f"{family}/{native}")
        if family == "anthropic":
            aliases.add(f"claude/{native}")
        if _model_namespace(native) == (family, native):
            aliases.add(native)
    return frozenset(_literal_fingerprint({**payload, "model": alias}) for alias in aliases)


async def _payload(request: Request, *, count_tokens: bool) -> dict[str, JsonValue] | JSONResponse:
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        return native_error(400, "Request body must be valid JSON")
    if not isinstance(payload, dict):
        return native_error(400, "Request body must be a JSON object")
    if not isinstance(payload.get("model"), str) or not payload["model"].strip():
        return native_error(400, "model must be a nonempty string")
    if not isinstance(payload.get("messages"), list):
        return native_error(400, "messages must be an array")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        return native_error(400, "stream must be a boolean")
    if count_tokens and payload.get("stream") is True:
        return native_error(400, "count_tokens does not stream")
    if not count_tokens:
        maximum = payload.get("max_tokens")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
            return native_error(400, "max_tokens must be a positive integer")
    return payload


def _request_continuations(payload: dict[str, JsonValue]) -> set[str]:
    tokens = native_continuation_tokens(payload)
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                tokens.update(native_continuation_tokens(message))
    return tokens


async def _handle(request: Request, *, count_tokens: bool) -> Response:
    try:
        api_key = await validate_gateway_api_key(request)
        payload = await _payload(request, count_tokens=count_tokens)
        if isinstance(payload, JSONResponse):
            return payload
        model = effective_model_for_api_key(api_key, str(payload["model"]))
        assert model is not None
        payload["model"] = model
        fingerprint = _fingerprint(payload)
        legacy_fingerprints = _legacy_fingerprints(payload)
        scope = api_key.id if api_key else "anonymous"
        tokens = _request_continuations(payload)
        if len(tokens) > 4096:
            return native_error(400, "Too many native continuation identities")
        session_token = (
            continuation_token("session", request.headers["x-session-id"])
            if request.headers.get("x-session-id")
            else None
        )
        bound: tuple[str, str] | None = None
        async with get_background_session() as session:
            repository = NativeOperationsRepository(session)
            if not count_tokens and await repository.has_uncertain_operation(
                scope, fingerprint, legacy_fingerprints=legacy_fingerprints
            ):
                return native_error(
                    400,
                    "This request is already in flight or has an uncertain upstream outcome; "
                    "it must not be replayed",
                    "invalid_request_error",
                )
            owners = await repository.resolve_continuation_owners(scope, tokens) if tokens else []
            if owners is None:
                raise NativeOwnershipError()
            if session_token:
                session_owners = await repository.resolve_continuation_owners(scope, {session_token})
                if session_owners:
                    owners.extend(session_owners)
            if owners:
                unique = set(owners)
                if len(unique) != 1:
                    raise NativeOwnershipError()
                bound = next(iter(unique))
        excluded: set[str] = set()
        if bound:
            async with get_background_session() as session:
                excluded.update(
                    source.id
                    for source in await ModelSourcesRepository(session).list_sources()
                    if source.id != bound[0]
                )
        message_excluded = set(excluded)
        if count_tokens and bound is None:
            async with get_background_session() as session:
                excluded.update(
                    source.id
                    for source in await ModelSourcesRepository(session).list_sources()
                    if not source.supports_count_tokens
                )
        last_rejection: NativeResponse | None = None
        for _ in range(1 if bound else MAX_UNACCEPTED_ATTEMPTS):
            source = await select_model_source(
                model,
                api_key,
                protocol="messages",
                require_streaming=payload.get("stream") is True,
                affinity_key=request.headers.get("x-session-id"),
                exclude_source_ids=excluded,
            )
            if source is None:
                if count_tokens and bound is None and last_rejection is None:
                    unsupported = await select_model_source(
                        model,
                        api_key,
                        protocol="messages",
                        exclude_source_ids=message_excluded,
                    )
                    if unsupported is not None:
                        return native_error(
                            501, "This model source does not support native count_tokens; no estimate is substituted"
                        )
                if last_rejection is not None:
                    return Response(
                        last_rejection.body, status_code=last_rejection.status, headers=last_rejection.headers
                    )
                return native_error(403, "No authorized eligible Messages source for this model", "permission_error")
            entry = selected_model_entry(source, model)
            if entry is None:
                raise ProviderError("Selected native source no longer contains the exact model")
            catalog_model = entry.model
            if bound and (source.id != bound[0] or native_source_principal(source) != bound[1]):
                raise NativeOwnershipError()
            if count_tokens and not source.supports_count_tokens:
                return native_error(
                    501, "This model source does not support native count_tokens; no estimate is substituted"
                )
            if not count_tokens and _source_cost_is_unavailable(source, api_key, catalog_model, request_model=model):
                return native_error(503, "Source pricing is required for this cost-limited key", "api_error")
            claims = try_claim(source)
            if claims is None:
                result = native_error(503, "Model source is busy", "overloaded_error")
                result.headers["retry-after"] = "1"
                return result
            try:
                max_tokens = 0
                if not count_tokens:
                    maximum = payload["max_tokens"]
                    assert isinstance(maximum, int)  # Validated by _payload.
                    max_tokens = maximum
                budget = ApiKeyRequestUsageBudget(
                    input_tokens=max(1, len(json.dumps(payload, ensure_ascii=True)) // 4),
                    output_tokens=max_tokens,
                )
                reservation = (
                    None
                    if count_tokens
                    else await _enforce_request_limits(
                        api_key,
                        request_model=model,
                        request_service_tier=None,
                        request_usage_budget=budget,
                    )
                )
                owner = NativeDispatch(
                    request=request,
                    source=source,
                    model=catalog_model,
                    api_key=api_key,
                    reservation=reservation,
                    claims=claims,
                    admission_budget=budget,
                    requested_service_tier=None,
                    settle_reservation=_settle_source_reservation,
                    release_reservation=_release_reservation,
                )
                claims.transfer_to(owner)
            except BaseException:
                claims.release_if_unowned()
                raise
            try:
                if not count_tokens:
                    async with get_background_session() as session:
                        owner.operation_id = await NativeOperationsRepository(session).claim_operation(
                            api_key.id if api_key else "anonymous",
                            fingerprint,
                            source.id,
                            model,
                            api_key=api_key,
                            require_streaming=payload.get("stream") is True,
                            expected_source=source,
                            legacy_fingerprints=legacy_fingerprints,
                        )
                    if owner.operation_id is None:
                        await owner.finish(status="error", error_code="native_request_uncertain")
                        return native_error(
                            400,
                            "This request is already in flight or has an uncertain upstream outcome; "
                            "it must not be replayed",
                            "invalid_request_error",
                        )
                    owner.start_heartbeat()
                if session_token and not count_tokens:
                    owner.operation_outcome = "rejected"
                    await owner.register_continuations({session_token})
                    owner.operation_outcome = "unknown"
                upstream_payload = dict(payload)
                upstream_payload["model"] = entry.upstream_model or entry.model

                async def open_owned() -> NativeResponse | SourceResponsesStream:
                    result = await open_native(
                        source,
                        upstream_payload,
                        incoming_headers=request.headers,
                        count_tokens=count_tokens,
                        query_string=request.url.query,
                        expected_principal=native_source_principal(source),
                    )
                    if isinstance(result, (NativeResponse, NativeStream)):
                        owner.response_headers = result.headers
                    if isinstance(result, SourceResponsesStream):
                        if owner.lease_lost:
                            await result.aclose()
                            raise aiohttp.ClientPayloadError("Native request lease expired during upstream open")
                        owner.stream = result
                    return result

                result = await open_with_disconnect_watch(request, owner, open_owned())
                if isinstance(result, SourceResponsesStream):
                    return SourceStreamingResponse(
                        _native_body(owner),
                        owner=owner,
                        headers={
                            **owner.response_headers,
                            "Cache-Control": "no-cache, no-transform",
                            "X-Accel-Buffering": "no",
                        },
                    )
                await owner.register_continuations(result.continuation_tokens)
                owner.operation_outcome = "completed"
                await owner.finish(status="success", usage=result.usage, upstream_status_code=result.status)
                return Response(result.body, status_code=result.status, headers=result.headers)
            except NativeRejected as exc:
                owner.response_headers = exc.response.headers
                owner.operation_outcome = "rejected" if exc.unaccepted else "unknown"
                await owner.finish(
                    status="error", error_code="native_upstream_rejected", upstream_status_code=exc.response.status
                )
                try:
                    await _await_cleanup_deferring_cancellation(
                        on_source_rejection(source, exc.response.status, exc.response.headers)
                    )
                except Exception:
                    logger.warning("native_source_rejection_write_failed source_id=%s", source.id, exc_info=True)
                if exc.unaccepted and bound is None and session_token is None:
                    excluded.add(source.id)
                    last_rejection = exc.response
                    continue
                return Response(exc.response.body, status_code=exc.response.status, headers=exc.response.headers)
            except ProviderError:
                owner.operation_outcome = "rejected"
                await owner.finish(status="error", error_code="source_credentials_unavailable")
                return native_error(503, "Source credentials are unavailable", "api_error")
            except ClientDisconnectedDuringOpen:
                await owner.abandon("client_disconnected_during_open")
                return native_error(499, "Client disconnected", "api_error")
            except (aiohttp.ClientError, TimeoutError):
                await owner.finish(status="error", error_code="native_request_uncertain")
                response = native_error(
                    502, "Upstream outcome is uncertain; this request must not be replayed", "api_error"
                )
                response.headers["x-should-retry"] = "false"
                return response
            except BaseException:
                await _await_cleanup_deferring_cancellation(owner.abandon("dispatch_interrupted"))
                raise
        assert last_rejection is not None
        return Response(last_rejection.body, status_code=last_rejection.status, headers=last_rejection.headers)
    except AppError as exc:
        kind = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}.get(
            exc.status_code, "api_error"
        )
        return native_error(exc.status_code, exc.message, kind)


@router.post("")
async def messages(request: Request) -> Response:
    return await _handle(request, count_tokens=False)


@router.post("/count_tokens")
async def count_tokens(request: Request) -> Response:
    return await _handle(request, count_tokens=True)
