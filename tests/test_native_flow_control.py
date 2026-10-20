"""Native boundary regressions: a real helper subprocess, no bridge changes.

The existing native unit helper writer supplies the version/capability handshake.
A send acknowledgement is our deterministic barrier: all preceding burst frames
have traversed stdout while the application consumer has not read a single one.
The ingress event/byte budgets are unchanged; arbitrary lossless stalls would
require per-stream credits in the Rust protocol, not blocking shared stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

import app.core.clients.native_egress as native
from app.core.clients.proxy_websocket import (
    NativeUpstreamWebSocket,
    UpstreamWebSocketTransportError,
    is_account_neutral_websocket_error_code,
)
from tests.unit.test_native_egress import _write_helper

pytestmark = pytest.mark.asyncio

_HELPER = r'''#!/usr/bin/env python3
import base64
import json
import sys

active = set()
accepted = {}
cancelled = 0

def emit(kind, request_id, **fields):
    print(json.dumps(dict(type=kind, request_id=request_id, **fields)), flush=True)

def ack(command):
    emit("websocket_sent", command["request_id"], command_id=command["command_id"])

for line in sys.stdin:
    command = json.loads(line)
    request_id = command["request_id"]
    kind = command["type"]
    if kind == "websocket_connect":
        active.add(request_id)
        emit("websocket_open", request_id, status=101, headers=[])
    elif kind == "websocket_send_text":
        text = command["text"]
        accepted[text] = accepted.get(text, 0) + 1
        if text.startswith("burst:"):
            _, count, mode = text.split(":")
            for index in range(int(count)):
                value = "%06d" % index
                if mode == "binary":
                    emit("websocket_binary", request_id, data=base64.b64encode(value.encode()).decode())
                elif mode == "interpreted":
                    value = json.dumps(dict(type="response.output_text.delta", delta=value))
                    emit("websocket_responses_text", request_id, text=value,
                         event_type="response.output_text.delta", payload=json.loads(value),
                         payload_response_id="response-burst", sequence_number=index)
                else:
                    emit("websocket_text", request_id, text=value)
            ack(command)
        elif text == "hold":
            # Accepted upstream operation with no ack. Cancellation must never
            # reconnect/replay it, even if a caller was waiting to close.
            emit("websocket_text", request_id, text="accepted:hold")
        elif text == "stats":
            emit("websocket_text", request_id, text=json.dumps(dict(accepted=accepted, cancelled=cancelled)))
            ack(command)
        elif text.startswith("diagnostic:"):
            ack(command)
            emit("websocket_error", request_id, message="Bearer SECRET https://user:PASS@host/?key=TOKEN",
                 failure_phase=text.split(":", 1)[1], retryable_same_contract=True,
                 headers=[["Authorization", "Bearer SECRET"]],
                 body=base64.b64encode(b"SECRET").decode())
            active.discard(request_id)
        else:
            emit("websocket_text", request_id, text=text)
            ack(command)
    elif kind == "websocket_send_binary":
        emit("websocket_binary", request_id, data=command["data"])
        ack(command)
    elif kind == "websocket_close":
        ack(command)
        emit("websocket_close", request_id, code=command["code"], reason=command["reason"])
        active.discard(request_id)
    elif kind == "cancel":
        cancelled += 1
        active.discard(request_id)
        emit("cancelled", request_id)
        # An independent stream observes cancellation without sleeps or polling.
        for other in sorted(active):
            emit("websocket_text", other, text="cancelled:" + request_id)
'''


@pytest.fixture
async def boundary(tmp_path):
    helper = tmp_path / "flow-helper"
    _write_helper(helper, _HELPER)
    client = native.SubprocessNativeEgressClient(helper)
    try:
        yield client
    finally:
        await asyncio.wait_for(client.aclose(), timeout=2)


async def _open(client):
    socket = await client.websocket(native.NativeWebSocketRequest(
        url="wss://example.invalid/flow", headers={}, connect_timeout_seconds=2, max_message_bytes=1024 * 1024,
    ))
    return socket, NativeUpstreamWebSocket(socket)


@pytest.mark.parametrize("mode", ["text", "binary", "interpreted"])
async def test_paused_consumer_keeps_512_frames_order_and_terminal_close(boundary, mode):
    socket, public = await _open(boundary)
    # Ack is deliberately emitted AFTER all frames. Awaiting send proves that
    # the full burst was dispatched without ever draining the public consumer.
    await asyncio.wait_for(public.send_text(f"burst:512:{mode}"), timeout=2)
    await asyncio.wait_for(public.close(code=1000, reason="done"), timeout=2)
    assert socket._events.empty()  # no hidden second backlog after ownership transfer
    for index in range(512):
        frame = await asyncio.wait_for(public.receive(), timeout=1)
        expected = f"{index:06d}"
        assert frame.kind == ("binary" if mode == "binary" else "text"), frame
        if mode == "binary":
            assert frame.data == expected.encode()
        elif mode == "interpreted":
            assert frame.payload["delta"] == expected
            assert frame.responses_interpreted
            assert frame.routing.sequence_number == index
            assert frame.routing.payload_response_id == "response-burst"
        else:
            assert frame.text == expected
    terminal = await asyncio.wait_for(public.receive(), timeout=1)
    assert (terminal.kind, terminal.close_code, terminal.close_reason) == ("close", 1000, "done")
    assert boundary._generation == 1


@pytest.mark.parametrize("event_limit,byte_limit", [(4, 1024), (4096, 24)])
async def test_saturated_consumer_preserves_prefix_bounds_and_isolates_cancel(
    boundary, monkeypatch, event_limit, byte_limit,
):
    monkeypatch.setattr(native, "_NATIVE_STREAM_QUEUE_LIMIT", event_limit)
    monkeypatch.setattr(native, "_NATIVE_STREAM_QUEUE_BYTES_LIMIT", byte_limit)
    stalled, left = await _open(boundary)
    _, right = await _open(boundary)
    with pytest.raises(UpstreamWebSocketTransportError) as caught:
        await asyncio.wait_for(left.send_text("burst:16:text"), timeout=2)
    error = caught.value
    assert error.failure_phase == "consumer_backpressure"
    assert error.error_code == "native_websocket_consumer_backpressure"
    assert is_account_neutral_websocket_error_code(error.error_code)
    assert stalled._messages.qsize() <= event_limit
    assert stalled._messages.queued_bytes <= byte_limit
    assert stalled._events.empty()
    # A bounded cancellation command progresses despite left's full mailbox.
    notification = await asyncio.wait_for(right.receive(), timeout=1)
    assert notification.text == f"cancelled:{stalled._request_id}"
    for index in range(4):
        assert (await left.receive()).text == f"{index:06d}"
    failure = await asyncio.wait_for(left.receive(), timeout=1)
    assert failure.kind == "error"
    assert failure.failure_phase == "consumer_backpressure"
    assert is_account_neutral_websocket_error_code(failure.error_code)
    assert stalled._messages.queued_bytes == 0
    await asyncio.wait_for(right.send_text("healthy"), timeout=1)
    assert (await right.receive()).text == "healthy"
    await right.send_text("stats")
    stats = json.loads((await right.receive()).text)
    assert stats["accepted"]["burst:16:text"] == 1
    assert stats["cancelled"] == 1
    assert boundary._generation == 1
    await asyncio.wait_for(right.close(), timeout=1)


async def test_paused_stream_and_many_sends_do_not_block_other_stream_ack_or_close(boundary):
    _, left = await _open(boundary)
    _, right = await _open(boundary)
    await asyncio.wait_for(left.send_text("burst:512:text"), timeout=2)
    # More than the helper's 32-command capacity, without any frame consumers.
    await asyncio.wait_for(asyncio.gather(*(right.send_text(str(i)) for i in range(48))), timeout=2)
    await asyncio.wait_for(right.send_bytes(b"binary"), timeout=1)
    await asyncio.wait_for(right.close(), timeout=1)
    for index in range(48):
        assert (await right.receive()).text == str(index)
    assert (await right.receive()).data == b"binary"
    assert (await right.receive()).close_code == 1000
    await asyncio.wait_for(left.close(), timeout=1)
    for index in range(512):
        assert (await left.receive()).text == f"{index:06d}"
    assert (await left.receive()).close_code == 1000


@pytest.mark.parametrize("closing", [False, True], ids=["cancel-send", "close-during-send"])
async def test_accepted_send_cancellation_is_prompt_and_never_replayed(boundary, monkeypatch, closing):
    socket, left = await _open(boundary)
    _, right = await _open(boundary)
    pending = asyncio.create_task(left.send_text("hold"))
    assert (await asyncio.wait_for(left.receive(), timeout=1)).text == "accepted:hold"
    if closing:
        monkeypatch.setattr(native, "_NATIVE_CANCEL_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(left.close(), timeout=0.5)
        with pytest.raises(UpstreamWebSocketTransportError) as caught:
            await asyncio.wait_for(pending, timeout=0.5)
        assert caught.value.failure_phase == "cancelled"
    else:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=0.5)
    assert (await asyncio.wait_for(right.receive(), timeout=1)).text == f"cancelled:{socket._request_id}"
    await asyncio.wait_for(right.send_text("stats"), timeout=1)
    stats = json.loads((await right.receive()).text)
    assert stats["accepted"]["hold"] == 1
    assert stats["cancelled"] == 1
    assert boundary._generation == 1
    await right.close()


@pytest.mark.parametrize("phase,expected", [
    ("transport", "transport"),
    ("liveness_timeout", "liveness_timeout"),
    ("helper_write", "helper_write"),
    ("consumer_backpressure", "consumer_backpressure"),
    ("Bearer SECRET https://user:PASS@host/?key=TOKEN", "unknown"),
])
async def test_public_diagnostics_preserve_only_allowlisted_phase(boundary, phase, expected):
    _, public = await _open(boundary)
    with contextlib.suppress(UpstreamWebSocketTransportError):
        await public.send_text("diagnostic:" + phase)
    result = await asyncio.wait_for(public.receive(), timeout=1)
    assert result.kind == "error"
    assert result.failure_phase == expected
    assert f"failure_phase={expected}" in result.error
    for secret in ("SECRET", "PASS", "TOKEN", "Bearer", "https://", "Authorization"):
        assert secret not in result.error
    if expected == "consumer_backpressure":
        assert result.error_code == "native_websocket_consumer_backpressure"
        assert is_account_neutral_websocket_error_code(result.error_code)
    assert boundary._generation == 1
