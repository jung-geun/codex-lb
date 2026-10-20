"""Behavioral native lifecycle tests against isolated local provider transports.

These fixtures are synthetic protocol peers, not evidence of real subscription
inference. No real credentials or provider endpoints are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, timedelta

import pytest
from aiohttp import web
from sqlalchemy import select

from app.core.auth.dependencies import validate_required_proxy_api_key_authorization
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import (
    ApiKeyUsageReservation,
    ApiKeyUsageReservationItem,
    ModelSourceModel,
    ModelSourceOAuthCredential,
    ModelSourceQuotaWindow,
    ModelSourceRequestOperation,
    RequestLog,
)
from app.db.session import SessionLocal
from app.modules.model_sources import selection, subscription_runtime
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.proxy.messages_api import NativeDispatch
from app.modules.proxy.source_admission import get_source_bulkhead
from app.modules.proxy.source_dispatch import SourceDispatch
from app.modules.quota_estimates.batch import _request_usage
from app.modules.quota_estimates.estimator import Observation, estimate_quota
from app.modules.request_logs.repository import RequestLogsRepository
from tests.integration.model_source_helpers import _AsgiStream, _enable_api_key_auth, stub_source_upstreams
from tests.simulation.virtual_time import VirtualClock

pytestmark = pytest.mark.integration
MODEL = "claude-native-lifecycle"


@pytest.fixture
async def native_upstreams() -> AsyncIterator:
    async with stub_source_upstreams() as start:
        yield start


async def _source(client, base_url, name, priority, *, timeout=10, count=True) -> str:
    response = await client.post(
        "/api/model-sources/",
        json={
            "name": name,
            "baseUrl": base_url,
            "apiKey": "upstream-" + name,
            "supportsChatCompletions": False,
            "supportsResponses": False,
            "supportsMessages": True,
            "supportsCountTokens": count,
            "priority": priority,
            "timeoutSeconds": timeout,
            "maxConcurrency": 1,
            "models": [
                {
                    "model": MODEL,
                    "upstreamModel": "claude-native-actual",
                    "contextWindow": 200000,
                    "maxOutputTokens": 32000,
                    "inputPer1M": 2,
                    "cachedInputPer1M": 0.2,
                    "outputPer1M": 10,
                    "cacheCreationPer1M": 2.5,
                    "cacheCreation1HPer1M": 4,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _key(client, sources, *, limited=False):
    created = await client.post(
        "/api/api-keys/",
        json={
            "name": "native-lifecycle",
            "assignedSourceIds": sources,
            "limits": (
                [{"limitType": "total_tokens", "limitWindow": "weekly", "maxValue": 1000000}] if limited else []
            ),
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["key"], created.json()["id"]


def _event(kind, **payload):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **payload}) + "\n\n").encode()


USAGE = {
    "input_tokens": 11,
    "output_tokens": 7,
    "cache_read_input_tokens": 20,
    "cache_creation_input_tokens": 5,
    "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 2},
}
CONTENT = [
    {"type": "thinking", "thinking": "private", "signature": "signed-A"},
    {"type": "tool_use", "id": "tool-A", "name": "lookup", "input": {"x": 1}},
]


def _message(content=CONTENT):
    return {
        "id": "msg_native_actual",
        "type": "message",
        "role": "assistant",
        "model": "claude-native-actual",
        "content": content,
        "stop_reason": "tool_use",
        "usage": USAGE,
    }


def _wire():
    return b"".join(
        [
            _event("message_start", message={**_message([]), "usage": {**USAGE, "output_tokens": 0}}),
            _event("ping"),
            _event("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""}),
            _event("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "private"}),
            _event("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "signed-"}),
            _event("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "A"}),
            _event("content_block_stop", index=0),
            _event(
                "content_block_start",
                index=1,
                content_block={"type": "tool_use", "id": "tool-A", "name": "lookup", "input": {}},
            ),
            _event("content_block_delta", index=1, delta={"type": "input_json_delta", "partial_json": '{"x":1}'}),
            _event("content_block_stop", index=1),
            _event("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 7}),
            _event("message_stop"),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_native_json_sse_and_second_turn_use_actual_account_owner(async_client, native_upstreams, streaming):
    await _enable_api_key_auth(async_client)
    seen_a, seen_b = [], []

    async def account_a(request):
        seen_a.append((request.path_qs, dict(request.headers), await request.json()))
        if request.path.endswith("/count_tokens"):
            return web.json_response({"input_tokens": 41})
        if seen_a[-1][2].get("stream"):
            return web.Response(body=_wire(), content_type="text/event-stream")
        return web.json_response(_message())

    async def account_b(request):
        seen_b.append(await request.read())
        return web.json_response(
            {"type": "error", "error": {"type": "api_error", "message": "wrong owner"}}, status=500
        )

    a = await _source(async_client, await native_upstreams(account_a), "A", 100)
    b = await _source(async_client, await native_upstreams(account_b), "B", 0)
    key, scope = await _key(async_client, [a, b], limited=True)
    payload = {
        "model": MODEL,
        "max_tokens": 100,
        "stream": streaming,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            }
        ],
        "thinking": {"type": "enabled", "budget_tokens": 64},
        "native_extension": {"keep": [1, 2]},
    }
    response = await async_client.post(
        "/v1/messages?beta=true",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "fixture-beta",
        },
        json=payload,
    )
    assert response.status_code == 200
    if streaming:
        assert response.content == _wire()
    else:
        assert response.json() == _message()
    path, headers, forwarded = seen_a[0]
    assert path == "/v1/messages?beta=true"
    assert headers["x-api-key"] == "upstream-A"
    assert headers["anthropic-beta"] == "fixture-beta"
    assert headers["anthropic-version"] == "2023-06-01"
    assert key not in json.dumps(headers)
    assert forwarded == {**payload, "model": "claude-native-actual"}
    # The second turn has no session/affinity header. Durable opaque identity,
    # not process-local affinity or current priority, must retain account A.
    changed = await async_client.patch(f"/api/model-sources/{b}", json={"priority": 200})
    assert changed.status_code == 200
    followup = {
        "model": MODEL,
        "max_tokens": 100,
        "messages": [
            {"role": "assistant", "content": CONTENT},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-A", "content": "found"}]},
        ],
    }
    response = await async_client.post("/v1/messages", headers={"authorization": f"Bearer {key}"}, json=followup)
    assert response.status_code == 200
    assert len(seen_a) == 2 and seen_b == []
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(RequestLog).where(RequestLog.api_key_id == scope).order_by(RequestLog.requested_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].model == MODEL and rows[0].served_model == "claude-native-actual"
        assert rows[0].input_tokens == 36 and rows[0].cached_input_tokens == 20
        assert rows[0].cache_creation_input_tokens == 5
        assert rows[0].cache_creation_5m_input_tokens == 3 and rows[0].cache_creation_1h_input_tokens == 2
        assert rows[0].cost_usd == pytest.approx((11 * 2 + 20 * 0.2 + 3 * 2.5 + 2 * 4 + 7 * 10) / 1000000)
        assert all(row.latency_ms is not None and row.latency_ms >= 0 for row in rows)
        assert all(row.latency_first_token_ms is None for row in rows)
        # Exercise the production log-to-quota adapter with real routed rows,
        # not warmup fixtures or zero-filled execution durations.
        requests = [_request_usage(row) for row in rows]
        left = min(request.started_at for request in requests) - timedelta(seconds=1)
        right = max(request.completed_at for request in requests) + timedelta(seconds=1)
        reset = right + timedelta(days=7)
        estimate = estimate_quota(
            window_key="seven_day",
            model=None,
            observations=[Observation(left, 0, reset), Observation(right, 0.08, reset)],
            requests=requests,
            now=right,
        )
        assert estimate.status == "estimated"
        assert estimate.estimate_kind == "empirical_estimate"
        assert estimate.observed_tokens == 86 and estimate.request_count == 2
        operations = (
            (
                await session.execute(
                    select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
                )
            )
            .scalars()
            .all()
        )
        assert len(operations) == 2 and all(operation.status == "completed" for operation in operations)
    assert get_source_bulkhead().in_flight(a) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_native_log_execution_interval_excludes_delayed_cleanup(
    async_client,
    native_upstreams,
    monkeypatch,
    streaming,
):
    await _enable_api_key_auth(async_client)
    clock = VirtualClock(monotonic_value=100, epoch_value=utcnow().replace(tzinfo=UTC).timestamp())
    expected_start = clock.now()
    expected_end = expected_start + timedelta(seconds=2)

    async def upstream(request):
        clock.advance(2)
        if (await request.json()).get("stream"):
            return web.Response(body=_wire(), content_type="text/event-stream")
        return web.json_response(_message())

    source = await _source(async_client, await native_upstreams(upstream), "delayed-cleanup", 0)
    key, scope = await _key(async_client, [source], limited=True)
    original_init = NativeDispatch.__post_init__
    original_stop = NativeDispatch.stop_heartbeat
    original_close = SourceDispatch.close_source
    original_settle = SourceDispatch.settle_or_release
    original_add = RequestLogsRepository.add_log

    def inject_clock(owner):
        original_init(owner)
        owner.clock = clock

    async def delayed_stop(owner):
        clock.advance(30)
        await original_stop(owner)

    async def delayed_close(owner):
        clock.advance(45)
        await original_close(owner)

    async def delayed_settle(owner, status, usage):
        clock.advance(60)
        await original_settle(owner, status, usage)

    async def delayed_add(repository, *args, **kwargs):
        clock.advance(90)
        return await original_add(repository, *args, **kwargs)

    monkeypatch.setattr(NativeDispatch, "__post_init__", inject_clock)
    monkeypatch.setattr(NativeDispatch, "stop_heartbeat", delayed_stop)
    monkeypatch.setattr(SourceDispatch, "close_source", delayed_close)
    monkeypatch.setattr(SourceDispatch, "settle_or_release", delayed_settle)
    monkeypatch.setattr(RequestLogsRepository, "add_log", delayed_add)
    response = await async_client.post(
        "/v1/messages",
        headers={"x-api-key": key},
        json={"model": MODEL, "max_tokens": 100, "stream": streaming, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog).where(RequestLog.api_key_id == scope))).scalar_one()
        assert row.latency_ms == 2000
        assert row.requested_at == expected_end.replace(tzinfo=None)
        usage = _request_usage(row)
        assert usage.started_at == expected_start
        assert usage.completed_at == expected_end
        assert clock.now() >= expected_end + timedelta(seconds=225)
        # The request really crossed this quota bracket's left edge. Cleanup
        # must not move it wholly inside and manufacture complete evidence.
        left = expected_start + timedelta(seconds=1)
        right = clock.now() + timedelta(seconds=1)
        reset = right + timedelta(days=7)
        estimate = estimate_quota(
            window_key="seven_day",
            model=None,
            observations=[Observation(left, 0, reset), Observation(right, 0.08, reset)],
            requests=[usage],
            now=right,
        )
        assert estimate.status == "insufficient" and estimate.reason == "boundary_overlap"


@pytest.mark.asyncio
async def test_count_tokens_is_real_native_operation_and_has_no_inference_reservation(async_client, native_upstreams):
    await _enable_api_key_auth(async_client)
    calls = []

    async def count(request):
        calls.append(request.path_qs)
        return web.json_response({"input_tokens": 41, "native_extension": "preserved"})

    source = await _source(async_client, await native_upstreams(count), "count", 0)
    key, scope = await _key(async_client, [source], limited=True)
    response = await async_client.post(
        "/v1/messages/count_tokens?beta=true",
        headers={"x-api-key": key},
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json() == {"input_tokens": 41, "native_extension": "preserved"}
    assert calls == ["/v1/messages/count_tokens?beta=true"]
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(ApiKeyUsageReservation).where(ApiKeyUsageReservation.api_key_id == scope)))
            .scalars()
            .all()
        )
        assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["partial", "timeout"])
async def test_native_unknown_never_fails_over_and_persisted_fence_blocks_replay(async_client, native_upstreams, mode):
    await _enable_api_key_auth(async_client)
    calls = {"A": 0, "B": 0}

    async def uncertain(request):
        calls["A"] += 1
        if mode == "timeout":
            await asyncio.sleep(2)
            return web.json_response(_message())
        return web.Response(
            body=_event("message_start", message={**_message([]), "usage": {**USAGE, "output_tokens": 0}})
            + _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "partial"}),
            content_type="text/event-stream",
        )

    async def other(request):
        calls["B"] += 1
        return web.json_response(_message())

    a = await _source(async_client, await native_upstreams(uncertain), "A", 100, timeout=1)
    b = await _source(async_client, await native_upstreams(other), "B", 0)
    key, scope = await _key(async_client, [a, b])
    payload = {
        "model": MODEL,
        "max_tokens": 20,
        "stream": mode == "partial",
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await async_client.post("/v1/messages", headers={"x-api-key": key}, json=payload)
    assert response.status_code == (200 if mode == "partial" else 502)
    if mode == "partial":
        assert b"partial" in response.content and b"event: message_stop" not in response.content
        assert b"event: error" in response.content
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].status == "unknown"
    replay = await async_client.post(
        "/v1/messages?beta=true",
        headers={"x-api-key": key, "x-request-id": "new-retry-id"},
        json={**payload, "stream": not payload["stream"]},
    )
    assert replay.status_code == 400
    assert calls == {"A": 1, "B": 0}
    assert get_source_bulkhead().in_flight(a) == 0


@pytest.mark.asyncio
async def test_native_never_started_stream_cancels_transport_and_releases_once(async_client, native_upstreams):
    await _enable_api_key_auth(async_client)
    transport_closed = asyncio.Event()
    hold = asyncio.Event()

    async def upstream(request):
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        try:
            await hold.wait()
        finally:
            transport_closed.set()
        return response

    source = await _source(
        async_client, await native_upstreams(upstream, handler_cancellation=True, shutdown_timeout=0.1), "cancel", 0
    )
    key, scope = await _key(async_client, [source], limited=True)
    stream = _AsgiStream(
        app=async_client._transport.app,
        path="/v1/messages",
        headers={"x-api-key": key},
        body=json.dumps({"model": MODEL, "max_tokens": 20, "stream": True, "messages": []}).encode(),
        stall_response_start=True,
    )
    task = asyncio.create_task(stream.run())
    try:
        await stream.wait_for_response_start()
        stream.disconnect()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(transport_closed.wait(), 5)
    finally:
        hold.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stream.chunks == []
    assert get_source_bulkhead().in_flight(source) == 0
    async with SessionLocal() as session:
        reservations = (
            (await session.execute(select(ApiKeyUsageReservation).where(ApiKeyUsageReservation.api_key_id == scope)))
            .scalars()
            .all()
        )
        assert len(reservations) == 1 and reservations[0].status == "released"
        operations = (
            (
                await session.execute(
                    select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
                )
            )
            .scalars()
            .all()
        )
        assert len(operations) == 1 and operations[0].status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("all_rejected", [False, True])
async def test_only_explicit_unaccepted_response_can_fail_over_and_errors_remain_safe(
    async_client, native_upstreams, all_rejected
):
    await _enable_api_key_auth(async_client)
    calls = {"A": 0, "B": 0}

    async def account_a(request):
        calls["A"] += 1
        return web.json_response(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "unaccepted"}}, status=429
        )

    async def account_b(request):
        calls["B"] += 1
        if all_rejected:
            return web.Response(
                body=b'{"type":"error","error":{"type":"rate_limit_error","message":"upstream-B"}}',
                status=429,
                content_type="application/json",
                headers={
                    "retry-after": "3",
                    "x-should-retry": "true",
                    "anthropic-ratelimit-unified-5h-status": "rejected",
                    "anthropic-ratelimit-unified-5h-reset": "1900000000",
                },
            )
        return web.json_response(_message([]))

    a = await _source(async_client, await native_upstreams(account_a), "A", 100)
    b = await _source(async_client, await native_upstreams(account_b), "B", 0)
    key, scope = await _key(async_client, [a, b])
    response = await async_client.post(
        "/v1/messages", headers={"x-api-key": key}, json={"model": MODEL, "max_tokens": 20, "messages": []}
    )
    assert response.status_code == (429 if all_rejected else 200)
    assert calls == {"A": 1, "B": 1}
    if all_rejected:
        assert response.json()["error"]["message"] == "[redacted]"
        assert response.headers["retry-after"] == "3"
        assert response.headers["x-should-retry"] == "true"
        assert response.headers["anthropic-ratelimit-unified-5h-status"] == "rejected"
    async with SessionLocal() as session:
        operations = (
            (
                await session.execute(
                    select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
                )
            )
            .scalars()
            .all()
        )
        assert len(operations) == 2
        assert {operation.status for operation in operations} == (
            {"rejected"} if all_rejected else {"rejected", "completed"}
        )
    assert get_source_bulkhead().in_flight(a) == get_source_bulkhead().in_flight(b) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{}, {"type": "message", "content": []}])
async def test_native_malformed_json200_preserves_uncertainty_fence(async_client, native_upstreams, invalid):
    await _enable_api_key_auth(async_client)
    calls = []

    async def incomplete(request):
        calls.append(request.path)
        return web.json_response(invalid)

    source = await _source(async_client, await native_upstreams(incomplete), "bad-json", 0)
    key, scope = await _key(async_client, [source])
    payload = {"model": MODEL, "max_tokens": 20, "messages": []}
    response = await async_client.post("/v1/messages", headers={"x-api-key": key}, json=payload)
    assert response.status_code == 502
    replay = await async_client.post("/v1/messages", headers={"x-api-key": key}, json=payload)
    assert replay.status_code == 400 and len(calls) == 1
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].status == "unknown"


@pytest.mark.asyncio
async def test_native_message_stop_completes_without_waiting_for_upstream_eof(async_client, native_upstreams):
    await _enable_api_key_auth(async_client)
    closed = asyncio.Event()
    hold = asyncio.Event()

    async def terminal_then_hold(request):
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        try:
            await response.write(_wire())
            await hold.wait()
        finally:
            closed.set()
        return response

    source = await _source(
        async_client,
        await native_upstreams(terminal_then_hold, handler_cancellation=True, shutdown_timeout=0.1),
        "terminal",
        0,
    )
    key, scope = await _key(async_client, [source])
    try:
        response = await asyncio.wait_for(
            async_client.post(
                "/v1/messages",
                headers={"x-api-key": key},
                json={
                    "model": MODEL,
                    "max_tokens": 100,
                    "stream": True,
                    "messages": [],
                },
            ),
            5,
        )
        assert response.status_code == 200 and response.content == _wire()
        await asyncio.wait_for(closed.wait(), 5)
    finally:
        hold.set()
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].status == "completed"
    assert get_source_bulkhead().in_flight(source) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["dispatching", "unknown"])
async def test_uncertain_native_turn_is_fenced_before_paid_source_selection(
    async_client, native_upstreams, monkeypatch, status
):
    import app.modules.proxy.messages_api as messages_api

    await _enable_api_key_auth(async_client)

    async def unused(request):
        pytest.fail("uncertain request must never reach any provider")

    source = await _source(async_client, await native_upstreams(unused), "paid-candidate", 100)
    key, scope = await _key(async_client, [source])
    payload = {"model": MODEL, "messages": [], "max_tokens": 100}
    async with SessionLocal() as session:
        session.add(ModelSourceRequestOperation(
            id="uncertain-native-turn",
            api_key_scope=scope,
            request_fingerprint=messages_api._fingerprint(payload),
            source_id="original-subscription",
            model=MODEL,
            status=status,
        ))
        await session.commit()

    async def no_selection(*args, **kwargs):
        pytest.fail("durable uncertainty must be checked before selecting a paid fallback")

    monkeypatch.setattr(messages_api, "select_model_source", no_selection)
    response = await async_client.post("/v1/messages", headers={"x-api-key": key}, json=payload)
    assert response.status_code == 400
    assert "must not be replayed" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_model,retry_model", [
    ("claude-native-actual", "anthropic/claude-native-actual"),
    ("anthropic/claude-native-actual", "claude-native-actual"),
])
@pytest.mark.parametrize("legacy", [False, True])
async def test_alias_changing_uncertain_subscription_retry_never_dispatches_paid_alternative(
    async_client, native_upstreams, monkeypatch, initial_model, retry_model, legacy
):
    await _enable_api_key_auth(async_client)
    calls = {"subscription": 0, "paid": 0}

    async def uncertain(request):
        calls["subscription"] += 1
        assert request.headers["authorization"] == "Bearer " + "synthetic-native-access"
        assert (await request.json())["model"] == "claude-native-actual"
        return web.Response(
            body=_event("message_start", message={**_message([]), "usage": {**USAGE, "output_tokens": 0}})
            + _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "accepted partial"}),
            content_type="text/event-stream",
        )

    async def paid(request):
        calls["paid"] += 1
        return web.json_response(_message([]))

    a = await _source(async_client, await native_upstreams(uncertain), "subscription", -100)
    b = await _source(async_client, await native_upstreams(paid), "paid", 100)
    now = utcnow()
    async with SessionLocal() as session:
        repository = ModelSourcesRepository(session)
        plan = await repository.get_by_id(a)
        api = await repository.get_by_id(b)
        assert plan is not None and api is not None
        plan.provider, plan.auth_type, plan.billing_mode = "claude", "claude_oauth", "subscription"
        plan.entitlement_status = "verified"
        plan.entitlement_verified_at = plan.quota_checked_at = now
        plan.extra_usage_enabled = False
        plan.models[0].subscription_eligible = True
        plan.models[0].subscription_restricted = False
        plan.oauth_credential = ModelSourceOAuthCredential(
            access_token_encrypted=TokenEncryptor().encrypt("synthetic-native-access"),
            expires_at=now + timedelta(hours=1), account_uuid="native-alias-account",
        )
        plan.quota_windows = [ModelSourceQuotaWindow(
            window_key="seven_day", used_ratio=0.2, observed_at=now, resets_at=now + timedelta(days=1),
        )]
        api.provider, api.billing_mode = "anthropic", "api_metered"
        api.api_fallback_only = True
        api.models[0].model = "anthropic/claude-native-actual"
        await session.commit()
    # Keep real source selection, credential headers and HTTP/SSE dispatch;
    # only OAuth refreshing is replaced for this isolated local protocol peer.
    monkeypatch.setattr(subscription_runtime, "fresh_credential", subscription_runtime.load_source)
    key, scope = await _key(async_client, [a, b])
    payload = {"model": initial_model, "max_tokens": 20, "stream": True, "messages": []}
    response = await async_client.post("/v1/messages", headers={"x-api-key": key}, json=payload)
    assert response.status_code == 200 and b"accepted partial" in response.content
    assert b"event: error" in response.content
    async with SessionLocal() as session:
        operations = (await session.execute(
            select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
        )).scalars().all()
        assert len(operations) == 1 and operations[0].status == "unknown"
        if legacy:
            # Simulate an already durable pre-upgrade literal-model ledger.
            operations[0].request_fingerprint = hashlib.sha256(json.dumps(
                {name: value for name, value in payload.items() if name != "stream"},
                sort_keys=True, ensure_ascii=True, separators=(",", ":"),
            ).encode()).hexdigest()
        plan = await ModelSourcesRepository(session).get_by_id(a)
        assert plan is not None
        plan.is_enabled = False
        await session.commit()
    api_key = await validate_required_proxy_api_key_authorization(f"Bearer {key}")
    alternative = await selection.select_model_source(retry_model, api_key, protocol="messages")
    assert alternative is not None and alternative.id == b
    replay = await async_client.post(
        "/v1/messages", headers={"x-api-key": key}, json={**payload, "model": retry_model, "stream": False},
    )
    assert replay.status_code == 400 and "must not be replayed" in replay.json()["error"]["message"]
    assert calls == {"subscription": 1, "paid": 0}
    async with SessionLocal() as session:
        operations = (await session.execute(
            select(ModelSourceRequestOperation).where(ModelSourceRequestOperation.api_key_scope == scope)
        )).scalars().all()
        assert len(operations) == 1 and operations[0].status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_output", [None, 0])
async def test_completed_native_text_missing_final_usage_keeps_null_log_and_estimates_budget(
    async_client, native_upstreams, caplog, final_output
):
    await _enable_api_key_auth(async_client)
    frames = [
        _event("message_start", message={**_message([]), "usage": {**USAGE, "output_tokens": 0}}),
        _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "completed answer"}),
        _event("message_delta", delta={"stop_reason": "end_turn"},
               **({"usage": {"output_tokens": final_output}} if final_output is not None else {})),
        _event("message_stop"),
    ]
    wire = b"".join(frames)

    async def completed(request):
        return web.Response(body=wire, content_type="text/event-stream")

    source = await _source(async_client, await native_upstreams(completed), "missing-final-usage", 0)
    key, scope = await _key(async_client, [source], limited=True)
    response = await async_client.post(
        "/v1/messages", headers={"x-api-key": key},
        json={"model": MODEL, "max_tokens": 20, "messages": [], "stream": True},
    )
    assert response.status_code == 200 and response.content == wire
    async with SessionLocal() as session:
        row = (await session.execute(select(RequestLog).where(RequestLog.api_key_id == scope))).scalar_one()
        assert row.status == "success"
        reservation = (await session.execute(
            select(ApiKeyUsageReservation).where(ApiKeyUsageReservation.api_key_id == scope)
        )).scalar_one()
        assert reservation.status == "finalized"
        item = (await session.execute(select(ApiKeyUsageReservationItem).where(
            ApiKeyUsageReservationItem.reservation_id == reservation.id,
        ))).scalar_one()
        if final_output is None:
            assert row.input_tokens is row.output_tokens is row.cost_usd is None
            assert item.actual_delta is not None and item.actual_delta > 36
        else:
            assert row.input_tokens == 36 and row.output_tokens == 0
            assert row.cached_input_tokens == 20 and row.cache_creation_input_tokens == 5
            assert item.actual_delta == 36
        operation = (await session.execute(select(ModelSourceRequestOperation).where(
            ModelSourceRequestOperation.api_key_scope == scope,
        ))).scalar_one()
        assert operation.status == "completed"
    assert ("cause=missing_usage" in caplog.text) is (final_output is None)


@pytest.mark.asyncio
async def test_native_continuation_rejects_api_key_to_anonymous_authority_change(async_client, native_upstreams):
    await _enable_api_key_auth(async_client)
    calls = []

    async def upstream(request):
        calls.append(dict(request.headers))
        return web.json_response(_message())

    source = await _source(async_client, await native_upstreams(upstream), "auth-mode", 0)
    key, scope = await _key(async_client, [source])
    response = await async_client.post(
        "/v1/messages", headers={"x-api-key": key}, json={"model": MODEL, "max_tokens": 20, "messages": []},
    )
    assert response.status_code == 200 and len(calls) == 1
    async with SessionLocal() as session:
        before = await ModelSourcesRepository(session).get_by_id(source)
        assert before is not None
        retained_ciphertext = before.api_key_encrypted
    changed = await async_client.patch(f"/api/model-sources/{source}", json={"authType": "none"})
    assert changed.status_code == 200
    async with SessionLocal() as session:
        after = await ModelSourcesRepository(session).get_by_id(source)
        assert after is not None and after.api_key_encrypted == retained_ciphertext
    response = await async_client.post(
        "/v1/messages", headers={"x-api-key": key}, json={
            "model": MODEL, "max_tokens": 20,
            "messages": [{"role": "assistant", "content": CONTENT}, {"role": "user", "content": "continue"}],
        },
    )
    assert response.status_code == 403 and "ownership" in response.json()["error"]["message"]
    assert len(calls) == 1
    async with SessionLocal() as session:
        operations = (await session.execute(select(ModelSourceRequestOperation).where(
            ModelSourceRequestOperation.api_key_scope == scope,
        ))).scalars().all()
        assert len(operations) == 1 and operations[0].status == "completed"


@pytest.mark.asyncio
async def test_native_mapping_replacement_with_constant_parent_timestamp_is_refused_before_dispatch(
    async_client, native_upstreams, monkeypatch
):
    import app.modules.proxy.messages_api as messages_api

    await _enable_api_key_auth(async_client)
    calls = []

    async def upstream(request):
        calls.append(await request.json())
        return web.json_response(_message([]))

    source = await _source(async_client, await native_upstreams(upstream), "mapping-race", 0)
    key, scope = await _key(async_client, [source])

    async def replace_after_selection(*args, **kwargs):
        selected = await selection.select_model_source(*args, **kwargs)
        assert selected is not None
        async with SessionLocal() as session:
            repository = ModelSourcesRepository(session)
            current = await repository.get_by_id(source)
            assert current is not None
            await repository.replace_models(current, [ModelSourceModel(
                model=MODEL, upstream_model="claude-different-native", is_enabled=True,
            )])
            assert current.updated_at == selected.updated_at
        return selected

    monkeypatch.setattr(messages_api, "select_model_source", replace_after_selection)
    response = await async_client.post(
        "/v1/messages", headers={"x-api-key": key}, json={"model": MODEL, "max_tokens": 20, "messages": []},
    )
    assert response.status_code == 503 and calls == []
    async with SessionLocal() as session:
        operations = (await session.execute(select(ModelSourceRequestOperation).where(
            ModelSourceRequestOperation.api_key_scope == scope,
        ))).scalars().all()
        assert operations == []
    assert get_source_bulkhead().in_flight(source) == 0
