"""Tests for the WebSocket model-source guard.

Model sources are only reachable from the HTTP request path, so the WebSocket
transport must refuse them. Two guards cover the two ways a turn can reach an
upstream:

* the connect guard, which refuses the frame in-band and arms a bounded
  authenticated-session hint so the next handshake returns HTTP ``426``;
* the reuse guard, which fails a later ``response.create`` that switches to a
  source-owned model on an already-open subscription upstream.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import WebSocket

import app.modules.model_sources.selection as source_selection
import app.modules.proxy._service.websocket.mixin as ws_mixin
from app.db.models import ModelSource, ModelSourceModel
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.selection import (
    effective_model_for_api_key,
    responses_model_is_source_owned,
)
from app.modules.proxy import service as proxy_service
from tests.unit.test_proxy_utils import (
    _make_account,
    _make_proxy_settings,
    _QueuedTestUpstreamWebSocket,
    _repo_factory,
    _RequestLogsRecorder,
    _SettingsCache,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_source_transport_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import OrderedDict

    import app.modules.proxy._service.support as support

    monkeypatch.setattr(support, "_source_http_required", OrderedDict())
    monkeypatch.setattr(support, "_source_http_retry_evidence", OrderedDict())


def _api_key(*, enforced_model: str | None = None) -> ApiKeyData:
    from datetime import datetime

    return ApiKeyData(
        id="key_ws_guard",
        name="ws guard",
        key_prefix="sk-test-ws-guard",
        allowed_models=[],
        enforced_model=enforced_model,
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=datetime(2026, 1, 1),
        last_used_at=None,
    )


def _request_state(model: str) -> ws_mixin._WebSocketRequestState:
    return proxy_service._WebSocketRequestState(
        request_id="req-ws-guard",
        model=model,
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=anyio.current_time(),
    )


def test_effective_model_prefers_enforced_model() -> None:
    assert effective_model_for_api_key(None, "gpt-5.6-sol") == "gpt-5.6-sol"
    assert effective_model_for_api_key(_api_key(), "gpt-5.6-sol") == "gpt-5.6-sol"
    assert effective_model_for_api_key(_api_key(enforced_model="qwen3.8-max"), "gpt-5.6-sol") == "qwen3.8-max"




async def _run_connect_guard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_source_owned: bool,
    api_key: ApiKeyData | None = None,
    request_state_api_key: ApiKeyData | None = None,
):
    """Drive ``_connect_proxy_websocket`` far enough to observe the connect guard.

    ``_select_websocket_connect_account`` stands in for the failover loop the
    guard short-circuits, so reaching it means the guard did not fire.
    """
    emitted: dict[str, object] = {}
    selection_calls = 0
    seen_api_keys: list[ApiKeyData | None] = []

    async def fake_is_source_owned(model, key, *, raw_model=None):  # noqa: ANN001
        seen_api_keys.append(key)
        return is_source_owned

    async def fake_emit(self, websocket, **kwargs):  # noqa: ANN001
        emitted.update(kwargs)

    async def fake_select(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        nonlocal selection_calls
        selection_calls += 1
        return None

    settings = _make_proxy_settings()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(ws_mixin, "responses_model_is_source_owned", fake_is_source_owned)
    monkeypatch.setattr(proxy_service.ProxyService, "_emit_websocket_connect_failure", fake_emit)
    monkeypatch.setattr(proxy_service.ProxyService, "_select_websocket_connect_account", fake_select)

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    request_state = _request_state("qwen3.8-max")
    request_state.api_key = request_state_api_key

    account, upstream = await service._connect_proxy_websocket(
        {},
        sticky_key=None,
        sticky_kind=None,
        prefer_earlier_reset=False,
        routing_strategy="capacity_weighted",
        model="qwen3.8-max",
        request_state=request_state,
        api_key=api_key,
        client_send_lock=anyio.Lock(),
        websocket=AsyncMock(headers={}),
    )
    return account, upstream, emitted, selection_calls, seen_api_keys


@pytest.mark.asyncio
async def test_connect_guard_fails_session_for_source_owned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    account, upstream, emitted, selection_calls, _ = await _run_connect_guard(monkeypatch, is_source_owned=True)

    assert account is None
    assert upstream is None
    assert selection_calls == 0, "the guard must short-circuit before account selection"
    assert emitted["error_code"] == "model_source_requires_http_transport"
    assert emitted["status_code"] == 503, "the current accepted socket receives an in-band refusal"
    assert emitted["account_id"] is None


@pytest.mark.asyncio
async def test_connect_guard_ignores_subscription_models(monkeypatch: pytest.MonkeyPatch) -> None:
    account, _upstream, emitted, selection_calls, _ = await _run_connect_guard(monkeypatch, is_source_owned=False)

    assert account is None  # the stubbed selector returns no account
    assert selection_calls >= 1, "subscription models must proceed to account selection"
    assert emitted == {}


@pytest.mark.asyncio
async def test_connect_guard_uses_the_per_request_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A policy refresh mid-session must not be judged against the stale session key.

    ``request_state.api_key`` is refreshed per request; the session key captured
    at connect time can be arbitrarily old on a long-lived socket, and the reuse
    guard already consults the fresh one.
    """
    session_key = _api_key()
    refreshed_key = _api_key(enforced_model="qwen3.8-max")

    *_, seen_api_keys = await _run_connect_guard(
        monkeypatch,
        is_source_owned=True,
        api_key=session_key,
        request_state_api_key=refreshed_key,
    )

    assert seen_api_keys == [refreshed_key]


def _text_frame(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        kind="text",
        text=json.dumps(payload, separators=(",", ":")),
        data=None,
        close_code=None,
        error=None,
        error_code=None,
    )


def _completed_turn(response_id: str) -> list[SimpleNamespace]:
    return [
        _text_frame({"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}),
        _text_frame(
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            }
        ),
    ]


class _Downstream:
    """A downstream socket that replays a scripted sequence of client frames."""

    def __init__(self, request_texts: list[str]) -> None:
        self.pending = list(request_texts)
        self.done = asyncio.Event()
        self.sent_text: list[str] = []
        self.turn_completed = asyncio.Event()

    async def receive(self) -> dict[str, object]:
        if self.pending:
            # Wait for the previous turn to settle so the frames stay ordered.
            if len(self.pending) < 1 or self.sent_text:
                await self.turn_completed.wait()
                self.turn_completed.clear()
            return {"type": "websocket.receive", "text": self.pending.pop(0)}
        await self.done.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        payload = json.loads(text)
        if payload.get("type") in {"response.completed", "response.failed", "error"}:
            self.turn_completed.set()
            if not self.pending:
                self.done.set()

    async def send_bytes(self, _data: bytes) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        del code, reason
        self.done.set()


def _create_frame(model: str) -> str:
    return json.dumps(
        {
            "type": "response.create",
            "model": model,
            "instructions": "",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "stream": True,
        },
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_first_turn_reaches_connect_guard_not_the_reuse_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh socket must reach the connect guard, which refuses before dispatch.

    The per-frame reuse guard runs before connection, so if it were not gated on
    an already-open upstream it would emit a terminal ``invalid_request_error``
    for the very first ``response.create`` and preempt the fallback, leaving
    model sources unreachable.
    """
    settings = _make_proxy_settings()
    settings.stream_idle_timeout_seconds = 300.0
    settings.proxy_downstream_websocket_idle_timeout_seconds = 120.0
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    account = _make_account("acc_ws_source_guard_first_turn")
    upstream = _QueuedTestUpstreamWebSocket(_completed_turn("resp_first_turn"))

    connect_called = False

    async def fake_connect(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        nonlocal connect_called
        connect_called = True
        return account, upstream

    async def always_source_owned(*args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        return True

    monkeypatch.setattr(ws_mixin, "responses_model_is_source_owned", always_source_owned)
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", fake_connect)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", AsyncMock(return_value=None))

    downstream = _Downstream([_create_frame("qwen3.8-max")])

    await service.proxy_responses_websocket(
        cast(WebSocket, downstream),
        {},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        api_key=None,
    )

    assert connect_called, "first turn must reach the connect path, not the per-frame reuse guard"
    assert not any("model_source_requires_http_transport" in text for text in downstream.sent_text), (
        "the reuse guard must not preempt the connect-path 503 on a fresh socket"
    )


@pytest.mark.asyncio
async def test_reuse_guard_rejects_a_later_source_owned_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second turn that switches to a source-owned model must not be forwarded.

    Socket reuse skips connection entirely, so without the reuse guard the frame
    would go to the subscription account already attached to the open upstream
    and be rejected by the backend with the unsupported-model error.
    """
    settings = _make_proxy_settings()
    settings.stream_idle_timeout_seconds = 300.0
    settings.proxy_downstream_websocket_idle_timeout_seconds = 120.0
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    account = _make_account("acc_ws_source_guard_reuse")
    upstream = _QueuedTestUpstreamWebSocket(_completed_turn("resp_turn_one"))

    async def fake_connect(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return account, upstream

    # Only the second turn's model is source-owned.
    async def source_owned_for_qwen(model, _api_key, *, raw_model=None):  # noqa: ANN001
        return model == "qwen3.8-max"

    released = AsyncMock()

    monkeypatch.setattr(ws_mixin, "responses_model_is_source_owned", source_owned_for_qwen)
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", fake_connect)
    monkeypatch.setattr(proxy_service.ProxyService, "_release_websocket_request_state_reservation", released)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", AsyncMock(return_value=None))

    downstream = _Downstream([_create_frame("gpt-5.6-sol"), _create_frame("qwen3.8-max")])

    await service.proxy_responses_websocket(
        cast(WebSocket, downstream),
        {},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        api_key=None,
    )

    assert any("resp_turn_one" in text for text in downstream.sent_text), "the subscription turn must complete"
    assert any("model_source_requires_http_transport" in text for text in downstream.sent_text), (
        "the source-owned turn must be rejected by the reuse guard"
    )
    assert len(upstream.sent_text) == 1, "the rejected turn must not be forwarded upstream"
    assert released.await_count >= 1, "the rejected turn must release its usage reservation"


def _alias_allowlist_api_key() -> ApiKeyData:
    """A key that allowlists exactly the alias an alias-named source exposes.

    ``validate_model_access`` resolves aliases on both sides, so this key also
    admits plain ``gpt-5`` requests — but ``select_responses_model_source``
    filters candidates against the allowlist *exactly*, which keeps the
    normalized ``gpt-5`` candidate away from source lookup. That makes the raw
    alias the only candidate that can match the source, in the unit fake, the
    integration database, and production alike.
    """
    from datetime import datetime

    return ApiKeyData(
        id="key_ws_guard_alias",
        name="ws guard alias",
        key_prefix="sk-test-ws-alias",
        allowed_models=["gpt-5-high"],
        enforced_model=None,
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=datetime(2026, 1, 1),
        last_used_at=None,
    )


class _AliasSourceCatalog:
    """Fake inventory I/O while real ownership and WebSocket guards execute."""

    def __init__(
        self, source_models: set[str], disabled_source_models: set[str] | None = None,
        *, messages_only: bool = False,
    ) -> None:
        self.source_models = source_models
        self.disabled_source_models = disabled_source_models or set()
        self.messages_only = messages_only

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        catalog = self

        class _FakeRepository:
            def __init__(self, _session: object) -> None:
                pass

            async def list_sources(self) -> list[ModelSource]:
                return [
                    ModelSource(
                        id="src_" + model, name=model, is_enabled=model in catalog.source_models,
                        provider="anthropic" if catalog.messages_only else "custom",
                        billing_mode="api_metered" if catalog.messages_only else "unknown",
                        supports_responses=not catalog.messages_only,
                        supports_messages=catalog.messages_only,
                        models=[ModelSourceModel(
                            model=model,
                            upstream_model=model.partition("/")[2] if catalog.messages_only else None,
                            is_enabled=True, supports_streaming=True,
                        )],
                    )
                    for model in catalog.source_models | catalog.disabled_source_models
                ]

        @asynccontextmanager
        async def fake_session():  # noqa: ANN202
            yield object()

        monkeypatch.setattr(source_selection, "ModelSourcesRepository", _FakeRepository)
        monkeypatch.setattr(source_selection, "get_background_session", fake_session)
        monkeypatch.setattr(source_selection, "detach_session_objects", lambda _session: None)


class _TurnDrivenUpstream:
    """An upstream that releases each scripted turn only after its request.

    ``_QueuedTestUpstreamWebSocket`` queues every frame up front, which would
    let a second turn's events race ahead of the second ``response.create``.
    Here turn N's events become readable only after the Nth upstream send, so
    a turn that is (correctly) rejected before forwarding leaves its events
    unread, and a (buggy) forwarded turn completes cleanly instead of hanging
    the session — the pre-fix failure stays a crisp assertion failure.
    """

    def __init__(self, turns: list[list[SimpleNamespace]]) -> None:
        self._turns = list(turns)
        self._messages: asyncio.Queue[SimpleNamespace] = asyncio.Queue()
        self.close_seen = asyncio.Event()
        self.sent_text: list[str] = []

    def response_header(self, name: str) -> str | None:
        del name
        return None

    async def receive(self) -> SimpleNamespace:
        message = await self._messages.get()
        if message.kind == "close":
            self.close_seen.set()
        return message

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        if self._turns:
            for event in self._turns.pop(0):
                self._messages.put_nowait(event)

    async def send_bytes(self, _data: bytes) -> None:
        return None

    async def close(self) -> None:
        self.close_seen.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["gpt-5-high", "claude-guard", "anthropic/claude-guard"])
async def test_reuse_guard_rejects_source_aliases_and_messages_only_provider(
    monkeypatch: pytest.MonkeyPatch, requested: str,
) -> None:
    """A later source-owned turn must never reuse a live Codex upstream."""
    settings = _make_proxy_settings()
    settings.stream_idle_timeout_seconds = 300.0
    settings.proxy_downstream_websocket_idle_timeout_seconds = 120.0
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    catalog = _AliasSourceCatalog(
        {"gpt-5-high" if requested == "gpt-5-high" else "anthropic/claude-guard"},
        messages_only=requested != "gpt-5-high",
    )
    catalog.install(monkeypatch)

    api_key = _alias_allowlist_api_key() if requested == "gpt-5-high" else _api_key()
    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    account = _make_account("acc_ws_source_guard_alias")
    upstream = _TurnDrivenUpstream([_completed_turn("resp_turn_one"), _completed_turn("resp_turn_two")])

    async def fake_connect(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return account, upstream

    async def fake_refresh(key):  # noqa: ANN001, ANN202
        return api_key

    released = AsyncMock()
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", fake_connect)
    monkeypatch.setattr(service, "_refresh_websocket_api_key_policy", fake_refresh)
    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_service.ProxyService, "_release_websocket_request_state_reservation", released)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", AsyncMock(return_value=None))

    downstream = _Downstream([_create_frame("gpt-5"), _create_frame(requested)])

    await service.proxy_responses_websocket(
        cast(WebSocket, downstream),
        {},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        api_key=api_key,
    )

    assert any("resp_turn_one" in text for text in downstream.sent_text), "the subscription turn must complete"
    assert any("model_source_requires_http_transport" in text for text in downstream.sent_text), (
        "the alias-owned turn must be rejected by the reuse guard"
    )
    assert len(upstream.sent_text) == 1, "the alias turn must not be forwarded to the subscription upstream"
    assert released.await_count >= 1, "the rejected turn must release its usage reservation"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["gpt-5-high", "claude-guard", "anthropic/claude-guard"])
async def test_connect_guard_rejects_source_aliases_and_messages_only_provider(
    monkeypatch: pytest.MonkeyPatch, requested: str,
) -> None:
    """First-turn ownership must precede any Codex connection or dispatch."""
    settings = _make_proxy_settings()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    catalog = _AliasSourceCatalog(
        {"gpt-5-high" if requested == "gpt-5-high" else "anthropic/claude-guard"},
        messages_only=requested != "gpt-5-high",
    )
    catalog.install(monkeypatch)

    api_key = _alias_allowlist_api_key() if requested == "gpt-5-high" else _api_key()
    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))

    emitted: dict[str, object] = {}
    selection_calls = 0

    async def fake_refresh(key):  # noqa: ANN001, ANN202
        return api_key

    async def fake_emit(self, websocket, **kwargs):  # noqa: ANN001, ANN003, ANN202
        emitted.update(kwargs)

    async def fake_select(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        nonlocal selection_calls
        selection_calls += 1
        return None

    monkeypatch.setattr(service, "_refresh_websocket_api_key_policy", fake_refresh)
    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_service.ProxyService, "_emit_websocket_connect_failure", fake_emit)
    monkeypatch.setattr(proxy_service.ProxyService, "_select_websocket_connect_account", fake_select)

    prepared = await service._prepare_websocket_response_create_request(
        json.loads(_create_frame(requested)),
        headers={},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        sticky_threads_enabled=False,
        openai_cache_affinity_max_age_seconds=0,
        api_key=api_key,
    )

    account, upstream = await service._connect_proxy_websocket(
        {},
        sticky_key=None,
        sticky_kind=None,
        prefer_earlier_reset=False,
        routing_strategy="capacity_weighted",
        # Exactly what the session loop passes: the normalized model.
        model=prepared.request_state.model,
        request_state=prepared.request_state,
        api_key=api_key,
        client_send_lock=anyio.Lock(),
        websocket=AsyncMock(headers={}),
    )

    assert account is None
    assert upstream is None
    assert selection_calls == 0, "the connect guard must short-circuit before account selection"
    assert emitted.get("error_code") == "model_source_requires_http_transport"
    assert emitted.get("status_code") == 503


def _create_frame_with_input(model: str, input_items: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "type": "response.create",
            "model": model,
            "instructions": "",
            "input": input_items,
            "stream": True,
        },
        separators=(",", ":"),
    )


def _input_file_frame(model: str, file_id: str) -> str:
    return _create_frame_with_input(
        model,
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "summarize the attachment"},
                    {"type": "input_file", "file_id": file_id},
                ],
            }
        ],
    )


def _compaction_trigger_frame(model: str) -> str:
    return _create_frame_with_input(
        model,
        [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "compaction_trigger"},
        ],
    )


class _TurnSerializedDownstream:
    """A downstream that sends each next frame only after the prior turn ends.

    ``_Downstream`` hands the session loop the next frame as soon as it asks,
    so a second turn can be dispatched while the first turn's events are still
    in flight; the first turn's terminal frame then arrives with no pending
    client frames left and ends the session before the second turn's events
    come back. The rejection tests never notice — the guard fails the second
    turn synchronously inside the message loop — but the forwarding
    regressions below need the second turn's scripted upstream events to reach
    the client, so this downstream serializes turns the way a real Codex
    client does: it waits for a terminal frame before sending the next
    ``response.create``.
    """

    def __init__(self, request_texts: list[str]) -> None:
        self.pending = list(request_texts)
        self.done = asyncio.Event()
        self.sent_text: list[str] = []
        self.turn_completed = asyncio.Event()
        self._dispatched_any = False

    async def receive(self) -> dict[str, object]:
        if self.pending:
            if self._dispatched_any:
                await self.turn_completed.wait()
                self.turn_completed.clear()
            self._dispatched_any = True
            return {"type": "websocket.receive", "text": self.pending.pop(0)}
        await self.done.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        payload = json.loads(text)
        if payload.get("type") in {"response.completed", "response.failed", "error"}:
            self.turn_completed.set()
            if not self.pending:
                self.done.set()

    async def send_bytes(self, _data: bytes) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        del code, reason
        self.done.set()


@pytest.mark.asyncio
async def test_reuse_guard_forwards_a_pinned_input_file_turn(db_setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later source-owned turn that references an uploaded file must be forwarded.

    The HTTP route skips source selection whenever the input references an
    ``input_file`` — the upload is account-scoped, so the request is pinned to
    the subscription account that received it. The equivalent WebSocket turn
    must reach that account through the owner-routing path instead of being
    failed by the reuse guard (regression for the source-routing-exclusions
    P2).
    """
    settings = _make_proxy_settings()
    settings.stream_idle_timeout_seconds = 300.0
    settings.proxy_downstream_websocket_idle_timeout_seconds = 120.0
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    account = _make_account("acc_ws_source_guard_file_pin")
    upstream = _TurnDrivenUpstream([_completed_turn("resp_turn_one"), _completed_turn("resp_turn_two")])

    async def fake_connect(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return account, upstream

    # The second turn's model is source-owned, like the reuse-guard rejection test.
    async def source_owned_for_qwen(model, _api_key, *, raw_model=None):  # noqa: ANN001
        return model == "qwen3.8-max"

    monkeypatch.setattr(ws_mixin, "responses_model_is_source_owned", source_owned_for_qwen)
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", fake_connect)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", AsyncMock(return_value=None))

    await service._pin_file_account("file_ws_guard_pin", account.id)
    downstream = _TurnSerializedDownstream(
        [_create_frame("gpt-5.6-sol"), _input_file_frame("qwen3.8-max", "file_ws_guard_pin")]
    )

    await service.proxy_responses_websocket(
        cast(WebSocket, downstream),
        {},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        api_key=None,
    )

    assert not any("model_source_requires_http_transport" in text for text in downstream.sent_text), (
        "HTTP excludes file-referencing requests from source routing, so the "
        "reuse guard must not fail the file-pinned turn"
    )
    assert len(upstream.sent_text) == 2, "the file-pinned turn must be forwarded to the pinned subscription account"
    assert any("resp_turn_two" in text for text in downstream.sent_text), "the file-pinned turn must complete"


@pytest.mark.asyncio
async def test_reuse_guard_forwards_a_terminal_compaction_trigger_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal compaction-trigger turn must stay on the subscription upstream.

    The HTTP route serves terminal compaction triggers through the upstream
    compact flow on the turn's owner account and never source-routes them, so
    the reuse guard must not fail the equivalent WebSocket turn even when its
    model is also exposed by a source (regression for the
    source-routing-exclusions P2).
    """
    settings = _make_proxy_settings()
    settings.stream_idle_timeout_seconds = 300.0
    settings.proxy_downstream_websocket_idle_timeout_seconds = 120.0
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    account = _make_account("acc_ws_source_guard_compact")
    upstream = _TurnDrivenUpstream([_completed_turn("resp_turn_one"), _completed_turn("resp_turn_two")])

    async def fake_connect(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return account, upstream

    async def source_owned_for_qwen(model, _api_key, *, raw_model=None):  # noqa: ANN001
        return model == "qwen3.8-max"

    monkeypatch.setattr(ws_mixin, "responses_model_is_source_owned", source_owned_for_qwen)
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", fake_connect)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", AsyncMock(return_value=None))

    downstream = _TurnSerializedDownstream([_create_frame("gpt-5.6-sol"), _compaction_trigger_frame("qwen3.8-max")])

    await service.proxy_responses_websocket(
        cast(WebSocket, downstream),
        {},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        api_key=None,
    )

    assert not any("model_source_requires_http_transport" in text for text in downstream.sent_text), (
        "HTTP excludes terminal compaction triggers from source routing, so "
        "the reuse guard must not fail the compaction turn"
    )
    assert len(upstream.sent_text) == 2, "the compaction turn must be forwarded to the subscription upstream"
    assert any("resp_turn_two" in text for text in downstream.sent_text), "the compaction turn must complete"


async def _drive_prepared_request_into_connect_guard(
    monkeypatch: pytest.MonkeyPatch,
    frame: str,
):
    """Prepare ``frame`` for real and drive it into the real connect guard.

    Mirrors ``test_connect_guard_sees_the_raw_model_alias``: the alias catalog
    makes ``gpt-5-high`` genuinely source-owned, so before the exclusions fix
    the guard demonstrably fires for these frames — the stubbed account
    selector standing in for the failover loop proves the guard was skipped.
    """
    settings = _make_proxy_settings()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    catalog = _AliasSourceCatalog({"gpt-5-high"})
    catalog.install(monkeypatch)

    api_key = _alias_allowlist_api_key()
    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))

    emitted: dict[str, object] = {}
    selection_calls = 0

    async def fake_refresh(key):  # noqa: ANN001, ANN202
        return api_key

    async def fake_emit(self, websocket, **kwargs):  # noqa: ANN001, ANN003, ANN202
        emitted.update(kwargs)

    async def fake_select(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        nonlocal selection_calls
        selection_calls += 1
        return None

    monkeypatch.setattr(service, "_refresh_websocket_api_key_policy", fake_refresh)
    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_service.ProxyService, "_emit_websocket_connect_failure", fake_emit)
    monkeypatch.setattr(proxy_service.ProxyService, "_select_websocket_connect_account", fake_select)

    prepared = await service._prepare_websocket_response_create_request(
        json.loads(frame),
        headers={},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        sticky_threads_enabled=False,
        openai_cache_affinity_max_age_seconds=0,
        api_key=api_key,
    )

    account, upstream = await service._connect_proxy_websocket(
        {},
        sticky_key=None,
        sticky_kind=None,
        prefer_earlier_reset=False,
        routing_strategy="capacity_weighted",
        model=prepared.request_state.model,
        request_state=prepared.request_state,
        api_key=api_key,
        client_send_lock=anyio.Lock(),
        websocket=AsyncMock(headers={}),
    )
    return prepared, account, upstream, emitted, lambda: selection_calls


@pytest.mark.asyncio
async def test_connect_guard_skips_input_file_requests(db_setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """The connect guard must not bounce a file-referencing request to HTTP.

    An ``input_file`` reference excludes the request from source routing on
    the HTTP path — pinned or not, the upload lives on a subscription
    account — so the connect path must proceed to (owner-required) account
    selection instead of emitting the 503 fallback.
    """
    prepared, account, upstream, emitted, selection_calls = await _drive_prepared_request_into_connect_guard(
        monkeypatch,
        _input_file_frame("gpt-5-high", "file_ws_connect_unpinned"),
    )

    assert emitted == {}, "the connect guard must not emit the 503 HTTP-fallback failure"
    assert selection_calls() >= 1, "file-referencing requests must proceed to account selection"
    assert account is None  # the stubbed selector returns no account
    assert upstream is None
    assert prepared.request_state.source_route_excluded is True, (
        "preparation must record the HTTP source-route exclusion on the request state"
    )


@pytest.mark.asyncio
async def test_connect_guard_skips_terminal_compaction_trigger_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connect guard must not bounce a terminal compaction trigger to HTTP.

    HTTP serves these through the compact flow on the owner account and never
    source-routes them; the WebSocket connect path must likewise proceed to
    account selection.
    """
    prepared, account, upstream, emitted, selection_calls = await _drive_prepared_request_into_connect_guard(
        monkeypatch,
        _compaction_trigger_frame("gpt-5-high"),
    )

    assert emitted == {}, "the connect guard must not emit the 503 HTTP-fallback failure"
    assert selection_calls() >= 1, "compaction-trigger requests must proceed to account selection"
    assert account is None  # the stubbed selector returns no account
    assert upstream is None
    assert prepared.request_state.source_route_excluded is True, (
        "preparation must record the HTTP source-route exclusion on the request state"
    )


@pytest.mark.asyncio
async def test_source_ownership_counts_disabled_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model whose only source is disabled is still source-owned.

    The WebSocket transport cannot serve a source-owned model whether the
    source is enabled or not; dispatching it to a subscription account instead
    yields the opaque upstream 400. Recognizing disabled ownership makes the
    guards bounce the turn to HTTP, where the disabled-source denial answers
    503 ``model_source_disabled``.
    """
    catalog = _AliasSourceCatalog(set(), disabled_source_models={"qwen3.8-max"})
    catalog.install(monkeypatch)

    assert await responses_model_is_source_owned("qwen3.8-max", None) is True
    assert await responses_model_is_source_owned("model-nobody-configured", None) is False, (
        "a model no source claims must keep falling through to subscription selection"
    )


@pytest.mark.asyncio
async def test_connect_guard_fails_session_for_disabled_source_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connect guard must bounce a disabled-source model to HTTP.

    The real ``responses_model_is_source_owned`` runs against a catalog where
    the model's only source is disabled: without the ``only_disabled`` probe
    the guard misses, the turn dispatches to a subscription account, and the
    incident's entitlement 400 comes back on exactly this transport.
    """
    settings = _make_proxy_settings()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    catalog = _AliasSourceCatalog(set(), disabled_source_models={"qwen3.8-max"})
    catalog.install(monkeypatch)

    emitted: dict[str, object] = {}
    selection_calls = 0

    async def fake_emit(self, websocket, **kwargs):  # noqa: ANN001, ANN003, ANN202
        emitted.update(kwargs)

    async def fake_select(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        nonlocal selection_calls
        selection_calls += 1
        return None

    monkeypatch.setattr(proxy_service.ProxyService, "_emit_websocket_connect_failure", fake_emit)
    monkeypatch.setattr(proxy_service.ProxyService, "_select_websocket_connect_account", fake_select)

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))

    account, upstream = await service._connect_proxy_websocket(
        {},
        sticky_key=None,
        sticky_kind=None,
        prefer_earlier_reset=False,
        routing_strategy="capacity_weighted",
        model="qwen3.8-max",
        request_state=_request_state("qwen3.8-max"),
        api_key=None,
        client_send_lock=anyio.Lock(),
        websocket=AsyncMock(headers={}),
    )

    assert account is None
    assert upstream is None
    assert selection_calls == 0, "the guard must short-circuit before subscription account selection"
    assert emitted.get("error_code") == "model_source_requires_http_transport"
    assert emitted.get("status_code") == 503, "a 4xx is terminal client-side and would strand the HTTP fallback"


@pytest.mark.asyncio
async def test_reuse_guard_rejects_a_later_disabled_source_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later turn that switches to a disabled-source model must not be forwarded.

    Socket reuse bypasses connect-time selection, so without disabled-source
    recognition in the reuse guard the frame goes to the subscription account
    already attached to the open upstream and is rejected by the backend with
    the unsupported-model 400.
    """
    settings = _make_proxy_settings()
    settings.stream_idle_timeout_seconds = 300.0
    settings.proxy_downstream_websocket_idle_timeout_seconds = 120.0
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))

    catalog = _AliasSourceCatalog(set(), disabled_source_models={"qwen3.8-max"})
    catalog.install(monkeypatch)

    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    account = _make_account("acc_ws_source_guard_disabled_reuse")
    upstream = _QueuedTestUpstreamWebSocket(_completed_turn("resp_turn_one"))

    async def fake_connect(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return account, upstream

    released = AsyncMock()

    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", fake_connect)
    monkeypatch.setattr(proxy_service.ProxyService, "_release_websocket_request_state_reservation", released)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", AsyncMock(return_value=None))

    downstream = _Downstream([_create_frame("gpt-5.6-sol"), _create_frame("qwen3.8-max")])

    await service.proxy_responses_websocket(
        cast(WebSocket, downstream),
        {},
        codex_session_affinity=False,
        openai_cache_affinity=False,
        api_key=None,
    )

    assert any("resp_turn_one" in text for text in downstream.sent_text), "the subscription turn must complete"
    assert any("model_source_requires_http_transport" in text for text in downstream.sent_text), (
        "the disabled-source turn must be rejected by the reuse guard"
    )
    assert len(upstream.sent_text) == 1, "the rejected turn must not be forwarded upstream"
    assert released.await_count >= 1, "the rejected turn must release its usage reservation"


@pytest.mark.parametrize("path", ["/backend-api/codex/responses", "/v1/responses"])
@pytest.mark.parametrize("expired", [False, True])
def test_source_rejection_next_authenticated_handshake_requires_http_only_for_same_session(
    monkeypatch: pytest.MonkeyPatch, path: str, expired: bool,
) -> None:
    from dataclasses import replace

    from fastapi import FastAPI
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient, WebSocketDenialResponse
    from starlette.websockets import WebSocketDisconnect

    import app.modules.proxy._service.support as support
    import app.modules.proxy.api as proxy_api

    settings = _make_proxy_settings()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_api, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(support, "_source_http_required", support.OrderedDict())
    now = [100.0]
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(support, "_upstream_ws_transport_failure_at", None)
    catalog = _AliasSourceCatalog({"qwen3.8-max"})
    catalog.install(monkeypatch)
    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    select_account = AsyncMock(side_effect=AssertionError("source request must never dispatch to a Codex account"))
    monkeypatch.setattr(service, "_select_websocket_connect_account", select_account)
    keys = {"Bearer first": _api_key(), "Bearer second": replace(_api_key(), id="other-key")}

    async def authenticate(socket, **kwargs):
        key = keys.get(socket.headers.get("authorization"))
        return (key, None) if key is not None else (None, JSONResponse({"error": "unauthorized"}, status_code=401))

    async def guarded_session(socket, headers, *, api_key, **kwargs):
        try:
            frame = await socket.receive_json()
        except WebSocketDisconnect:
            return
        state = _request_state(frame["model"])
        state.api_key = api_key
        state.skip_request_log = True
        result = await service._connect_proxy_websocket(
            headers, sticky_key=None, sticky_kind=None, prefer_earlier_reset=False,
            routing_strategy="capacity_weighted", model=state.model, request_state=state,
            api_key=api_key, client_send_lock=anyio.Lock(), websocket=socket,
        )
        assert result == (None, None)
        await socket.close()

    monkeypatch.setattr(proxy_api, "_validate_proxy_websocket_request", authenticate)
    monkeypatch.setattr(service, "proxy_responses_websocket", guarded_session)
    app = FastAPI()
    app.include_router(proxy_api.ws_router)
    app.include_router(proxy_api.v1_ws_router)
    app.dependency_overrides[proxy_api.get_proxy_websocket_context] = lambda: SimpleNamespace(service=service)
    headers = {"authorization": "Bearer first", "session_id": "source-session", "thread-id": "thread-one"}
    with TestClient(app) as client:
        with client.websocket_connect(path, headers=headers) as socket:
            socket.send_text(_create_frame("qwen3.8-max"))
            assert "model_source_requires_http_transport" in socket.receive_text()
            echoed_turn = dict(socket.extra_headers or []).get(b"x-codex-turn-state", b"").decode()
        # Neither another key, another session, nor a sibling thread may be
        # downgraded by a source lookup on the first accepted connection.
        for changes in (
            {"authorization": "Bearer second"}, {"session_id": "other-session"}, {"thread-id": "thread-two"},
        ):
            with client.websocket_connect(path, headers={**headers, **changes}):
                pass
        # Capability handshakes must remain on their only supported transport,
        # and must not consume an ordinary source-transport hint.
        with client.websocket_connect(path, headers={
            **headers, proxy_api.CODEX_LB_REQUIRED_CAPABILITY_HEADER: "security_work_authorized",
        }):
            pass
        with pytest.raises(WebSocketDenialResponse) as unauthenticated:
            with client.websocket_connect(path, headers={**headers, "authorization": "Bearer invalid"}):
                pass
        assert unauthenticated.value.status_code == 401
        assert not support.upstream_websocket_transport_recently_failed()
        retry_headers = dict(headers)
        if echoed_turn:
            retry_headers["x-codex-turn-state"] = echoed_turn
        if expired:
            now[0] += support.SOURCE_HTTP_REQUIRED_TTL_SECONDS + 0.01
            with client.websocket_connect(path, headers=retry_headers):
                pass
        else:
            with pytest.raises(WebSocketDenialResponse) as denied:
                with client.websocket_connect(path, headers=retry_headers):
                    pass
            assert denied.value.status_code == 426
            assert "model_source_requires_http_transport" in denied.value.text
            # One-shot consumption does not pin future upstream transport.
            with client.websocket_connect(path, headers=retry_headers):
                pass
    select_account.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", [
    "source_route_excluded", "require_security_work_authorized", "preferred_account_id",
    "previous_response_owner_account_id", "previous_response_id", "hard_continuity_anchor",
    "payload_conversation_bound", "response_create_sent_at", "operation_dispatched", "replay_required_account_id",
    "response_create_attempt_count", "response_id", "operation_persisted_response_id", "downstream_visible",
])
async def test_source_http_hint_never_arms_for_owner_capability_or_dispatched_work(
    monkeypatch: pytest.MonkeyPatch, constraint: str,
) -> None:
    import app.modules.proxy._service.support as support

    monkeypatch.setattr(support, "_source_http_required", support.OrderedDict())
    state = _request_state("qwen3.8-max")
    setattr(state, constraint, True if constraint not in {
        "preferred_account_id", "previous_response_owner_account_id",
        "previous_response_id", "replay_required_account_id",
    } else "account-bound")
    headers = {"session_id": "same-session"}
    ws_mixin._mark_source_http_retry(SimpleNamespace(headers=headers), state, _api_key())
    assert not support.consume_source_websocket_http_required(headers, _api_key())


@pytest.mark.parametrize("headers,key", [({}, _api_key()), ({"session_id": "known"}, None)])
def test_source_http_hint_without_authenticated_identity_is_never_global(monkeypatch, headers, key) -> None:
    import app.modules.proxy._service.support as support

    monkeypatch.setattr(support, "_source_http_required", support.OrderedDict())
    support.mark_source_websocket_http_required(headers, key)
    assert not support._source_http_required


def test_source_http_hint_is_bounded_and_thread_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.proxy._service.support as support

    monkeypatch.setattr(support, "_source_http_required", support.OrderedDict())
    monkeypatch.setattr(support, "SOURCE_HTTP_REQUIRED_MAX_ENTRIES", 2)
    for thread in ("one", "two", "three"):
        support.mark_source_websocket_http_required({"session_id": "same-process", "thread-id": thread}, _api_key())
    assert len(support._source_http_required) == 2
    assert not support.consume_source_websocket_http_required(
        {"session_id": "same-process", "thread-id": "one"}, _api_key(),
    )
    assert support.consume_source_websocket_http_required(
        {"session_id": "same-process", "thread-id": "two"}, _api_key(),
    )


@pytest.mark.parametrize("path", ["/backend-api/codex/responses", "/v1/responses"])
@pytest.mark.parametrize("fence", [
    None, "bridge_owner", "sticky_owner", "unknown_placeholder", "expiry", "previous_response", "conversation",
    "native_recovered", "sticky_abandoned",
])
def test_native_ws_source_rejection_426_then_http_retry_reselects_with_only_ownerless_placeholder_exemption(
    monkeypatch: pytest.MonkeyPatch, path: str, fence: str | None,
) -> None:
    from fastapi import FastAPI
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient, WebSocketDenialResponse
    from starlette.websockets import WebSocketDisconnect

    import app.modules.proxy._service.support as support
    import app.modules.proxy.api as proxy_api
    import app.modules.proxy.source_fallback as fallback
    from app.modules.proxy.sticky_repository import StickyOwnerLookup
    from tests.unit.test_model_source_selection_policy import _api_source, _Catalog

    model = "gpt-5.4"
    registry = SimpleNamespace(get_models_with_fallback=lambda: {model: object()})
    for module in (source_selection, fallback, proxy_api):
        monkeypatch.setattr(module, "get_model_registry", lambda: registry)
    settings = _make_proxy_settings()
    for module in (proxy_service, proxy_api, fallback):
        monkeypatch.setattr(module, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(support, "_upstream_ws_transport_failure_at", None)
    now = [100.0]
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: now[0]))
    paid = _api_source("native-paid", model=model, provider="openai")
    catalog = _Catalog(sources=[paid])

    @asynccontextmanager
    async def catalog_session():
        yield None

    monkeypatch.setattr(source_selection, "get_background_session", catalog_session)
    monkeypatch.setattr(source_selection, "ModelSourcesRepository", lambda session: catalog)
    monkeypatch.setattr(source_selection, "detach_session_objects", lambda session: None)
    key = _api_key()
    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    native_rank = AsyncMock(return_value=None)
    monkeypatch.setattr(service._load_balancer, "eligible_account_routing_rank", native_rank)
    select_account = AsyncMock(side_effect=AssertionError("rejected source work must not dispatch to Codex"))
    monkeypatch.setattr(service, "_select_websocket_connect_account", select_account)
    bridge_owner = AsyncMock(return_value="owner" if fence == "bridge_owner" else None)
    monkeypatch.setattr(service, "_resolve_compact_turn_state_owner", bridge_owner)
    sticky_owner = AsyncMock(return_value=StickyOwnerLookup(
        account_id="owner" if fence == "sticky_owner" else None,
        continuity_abandoned=fence == "sticky_abandoned",
    ))

    @asynccontextmanager
    async def owner_repos():
        yield SimpleNamespace(sticky_sessions=SimpleNamespace(get_account_id_and_abandonment=sticky_owner))

    monkeypatch.setattr(service, "_repo_factory", owner_repos)

    async def authenticate(socket, **kwargs):
        assert socket.headers["authorization"] == "Bearer first"
        return key, None

    async def guarded_session(socket, headers, *, api_key, **kwargs):
        try:
            frame = await socket.receive_json()
        except WebSocketDisconnect:
            return
        state = _request_state(frame["model"])
        state.api_key, state.skip_request_log = api_key, True
        assert await service._connect_proxy_websocket(
            headers, sticky_key=None, sticky_kind=None, prefer_earlier_reset=False,
            routing_strategy="capacity_weighted", model=state.model, request_state=state,
            api_key=api_key, client_send_lock=anyio.Lock(), websocket=socket,
        ) == (None, None)
        await socket.close()

    # Isolate the response executors, not source eligibility or either route's
    # admission decision. No accepted work is replayed or sent upstream here.
    source_response = AsyncMock(return_value=JSONResponse({"route": "source", "id": paid.id}))
    native_response = AsyncMock(return_value=JSONResponse({"route": "native"}))
    monkeypatch.setattr(proxy_api, "_source_responses_response", source_response)
    monkeypatch.setattr(proxy_api, "_collect_responses", native_response)
    monkeypatch.setattr(proxy_api, "_disabled_model_source_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_api, "_rate_limit_headers_for_request", AsyncMock(return_value={}))
    monkeypatch.setattr(
        proxy_api, "_apply_api_key_enforcement_with_fast_mode_policy", AsyncMock(return_value=(False, False, None)),
    )
    monkeypatch.setattr(proxy_api, "_validate_proxy_websocket_request", authenticate)
    monkeypatch.setattr(service, "proxy_responses_websocket", guarded_session)
    app = FastAPI()
    app.include_router(proxy_api.ws_router)
    app.include_router(proxy_api.v1_ws_router)
    app.include_router(proxy_api.v1_router)
    context = SimpleNamespace(service=service)
    app.dependency_overrides[proxy_api.get_proxy_websocket_context] = lambda: context
    app.dependency_overrides[proxy_api.get_proxy_context] = lambda: context
    app.dependency_overrides[proxy_api.validate_proxy_api_key] = lambda: key
    headers = {"authorization": "Bearer first", "session_id": "native-source-session", "thread-id": "native-thread"}
    with TestClient(app) as client:
        with client.websocket_connect(path, headers=headers) as socket:
            echoed_turn = dict(socket.extra_headers or [])[b"x-codex-turn-state"].decode()
            socket.send_text(_create_frame(model))
            assert "model_source_requires_http_transport" in socket.receive_text()
        retry_headers = {**headers, "x-codex-turn-state": echoed_turn}
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(path, headers=retry_headers):
                pass
        assert denied.value.status_code == 426
        body = {"model": model, "instructions": "", "input": "hello"}
        if fence == "unknown_placeholder":
            retry_headers["x-codex-turn-state"] = "turn_" + "f" * 32
        elif fence == "expiry":
            now[0] += support.SOURCE_HTTP_REQUIRED_TTL_SECONDS + 0.01
        elif fence == "previous_response":
            body["previous_response_id"] = "resp_existing_owner"
        elif fence == "conversation":
            body["conversation"] = "conv_existing_owner"
        elif fence == "native_recovered":
            native_rank.return_value = 1
        response = client.post("/v1/responses", headers=retry_headers, json=body)
    assert response.status_code == 200
    assert response.json()["route"] == ("source" if fence is None else "native")
    assert source_response.await_count == (1 if fence is None else 0)
    assert native_response.await_count == (0 if fence is None else 1)
    if fence is None:
        assert source_response.await_args.kwargs["source"] is paid
        assert source_response.await_args.kwargs["request_model"] == model
        assert bridge_owner.await_args.kwargs == {"turn_state": echoed_turn, "api_key": key, "fail_on_missing": False}
        sticky_owner.assert_awaited_once()
    assert not support.upstream_websocket_transport_recently_failed()
    select_account.assert_not_awaited()
