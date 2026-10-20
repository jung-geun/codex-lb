"""Consumer regressions using the pinned upstream's real SQLite coordinator.

Only the upstream WebSocket transport is synthetic. The app lifespan, HTTP
routes, account selection, operation fencing, and terminal spool replay are real.
"""

from __future__ import annotations

import json
import socket
import time

import pytest
from sqlalchemy import select

import app.modules.proxy.service as proxy_service
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.db.models import HttpBridgeOperationRecord
from app.db.session import SessionLocal
from app.dependencies import get_proxy_service_for_app
from tests.integration.test_http_responses_bridge import (
    _FakeBridgeUpstreamWebSocket,
    _cleanup_http_bridge_sessions,  # noqa: F401 -- upstream autouse fixture
    _collect_sse_events,
    _encode_jwt,
    _install_bridge_settings_with_limits,
    _make_auth_json,
)

pytestmark = pytest.mark.integration

_CONSUMERS = [
    pytest.param("/backend-api/codex/responses", True, id="codex-sse"),
    pytest.param("/v1/responses", False, id="openai-json"),
]


class _OperationUpstream(_FakeBridgeUpstreamWebSocket):
    def __init__(self, number: int, *, outcome: str) -> None:
        super().__init__(f"resp_operation_{number}")
        self.outcome = outcome
        self.accepted_operations = 0

    async def send_text(self, text: str) -> None:
        payload = json.loads(text)
        if not payload.get("previous_response_id"):
            await super().send_text(text)
            return
        self.accepted_operations += 1
        if self.outcome == "disconnect":
            self.sent_text.append(text)
            response_id = f"{self.response_id_prefix}_{len(self.sent_text)}"
            for event in (
                {
                    "type": "response.created",
                    "response": {"id": response_id, "object": "response", "status": "in_progress"},
                },
                {"type": "response.output_text.delta", "response_id": response_id, "delta": "partial"},
            ):
                await self._messages.put(UpstreamWebSocketMessage("text", text=json.dumps(event)))
            await self._messages.put(UpstreamWebSocketMessage("close", close_code=1000))
            return
        await super().send_text(text)
        if self.outcome == "incomplete":
            # The inherited transport queues exactly created + completed, without
            # yielding to the reader between puts. Change only the upstream outcome.
            created = self._messages.get_nowait()
            terminal = self._messages.get_nowait()
            event = json.loads(terminal.text)
            event["type"] = "response.incomplete"
            event["response"]["status"] = "incomplete"
            event["response"]["incomplete_details"] = {"reason": "max_output_tokens"}
            self._messages.put_nowait(created)
            self._messages.put_nowait(UpstreamWebSocketMessage("text", text=json.dumps(event)))


async def _install_transport(async_client, app_instance, monkeypatch, *, outcome: str):
    # Startup registered this same instance in the real ring before our settings
    # overlay; a synthetic second owner would turn reconnects into ownership races.
    _install_bridge_settings_with_limits(monkeypatch, enabled=True, instance_id=socket.gethostname())
    auth = _make_auth_json("operation-uncertainty", "operation-uncertainty@example.invalid")
    auth["tokens"]["accessToken"] = _encode_jwt(
        {"sub": "operation-uncertainty", "exp": int(time.time()) + 86400}
    )
    imported = await async_client.post(
        "/api/accounts/import",
        files={"auth_json": ("auth.json", json.dumps(auth), "application/json")},
    )
    assert imported.status_code == 200, imported.text
    upstreams: list[_OperationUpstream] = []

    async def connect(*args, **kwargs):
        upstream = _OperationUpstream(len(upstreams), outcome=outcome)
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    service = get_proxy_service_for_app(app_instance)
    return imported.json()["accountId"], service, upstreams


async def _operation_snapshot(account_id: str, parent_id: str) -> dict:
    async with SessionLocal() as session:
        result = await session.execute(
            select(HttpBridgeOperationRecord.__table__).where(
                HttpBridgeOperationRecord.account_id == account_id,
                HttpBridgeOperationRecord.parent_response_id == parent_id,
            )
        )
        return dict(result.mappings().one())


async def _begin_anchored_request(async_client, path: str, *, stream: bool):
    headers = {"session_id": "operation-uncertainty-thread"}
    payload = {
        "model": "gpt-5.1",
        "instructions": "Return exactly OK.",
        "input": [{"role": "user", "content": "bootstrap"}],
        "stream": True,
    }
    first = await _collect_sse_events(async_client, path, json_body=payload, headers=headers)
    assert first[-1]["type"] == "response.completed", first
    parent_id = first[-1]["response"]["id"]
    return (
        {
            **payload,
            "input": [{"role": "user", "content": "next operation"}],
            "previous_response_id": parent_id,
            "stream": stream,
        },
        headers,
        parent_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stream", _CONSUMERS)
async def test_acknowledged_disconnect_blocks_http_duplicates_without_retry_hint(
    async_client, app_instance, monkeypatch, path: str, stream: bool
) -> None:
    account_id, service, upstreams = await _install_transport(
        async_client, app_instance, monkeypatch, outcome="disconnect"
    )
    payload, headers, parent_id = await _begin_anchored_request(async_client, path, stream=stream)
    if stream:
        events = await _collect_sse_events(async_client, path, json_body=payload, headers=headers)
        assert any(event["type"] == "response.created" for event in events), events
        assert any(event["type"] == "response.output_text.delta" for event in events), events
        assert events[-1]["type"] == "response.failed", events
        assert not any(event["type"] == "response.completed" for event in events)
    else:
        interrupted = await async_client.post(path, json=payload, headers=headers)
        assert interrupted.status_code == 502, interrupted.text
        assert interrupted.json()["error"]["code"] == "stream_incomplete", interrupted.text

    before = await _operation_snapshot(account_id, parent_id)
    assert before["state"] == "acknowledged"
    assert before["response_id"] is not None
    assert not before["event_spool_complete"]
    assert before["recovery_dispatch_count"] == 0
    spool_before = await service._durable_bridge.get_operation_events(operation_id=before["operation_id"])
    assert sum(upstream.accepted_operations for upstream in upstreams) == 1
    assert sum(len(upstream.sent_text) for upstream in upstreams) == 2  # bootstrap + accepted operation

    # Two real HTTP reattempts must hit the durable fence, not a short-lived
    # retry counter. They must neither reset the original row nor resend work.
    for _ in range(2):
        duplicate = await async_client.post(path, json=payload, headers=headers)
        assert duplicate.status_code == 400, duplicate.text
        error = duplicate.json()["error"]
        assert error["code"] == "upstream_operation_status_unknown", duplicate.text
        assert error["type"] == "invalid_request_error", duplicate.text
        assert "retry-after" not in duplicate.headers
        assert "retry_after_seconds" not in error
        assert "retry_after_seconds" not in duplicate.json()
        assert await _operation_snapshot(account_id, parent_id) == before
        assert await service._durable_bridge.get_operation_events(operation_id=before["operation_id"]) == spool_before
        assert sum(upstream.accepted_operations for upstream in upstreams) == 1
        assert sum(len(upstream.sent_text) for upstream in upstreams) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stream", _CONSUMERS)
@pytest.mark.parametrize("outcome", ["completed", "incomplete"])
async def test_terminal_operation_replays_complete_spool_without_dispatch(
    async_client, app_instance, monkeypatch, path: str, stream: bool, outcome: str
) -> None:
    account_id, service, upstreams = await _install_transport(
        async_client, app_instance, monkeypatch, outcome=outcome
    )
    payload, headers, parent_id = await _begin_anchored_request(async_client, path, stream=stream)
    if stream:
        original = await _collect_sse_events(async_client, path, json_body=payload, headers=headers)
        assert original[-1]["type"] == f"response.{outcome}", original
    else:
        response = await async_client.post(path, json=payload, headers=headers)
        assert response.status_code == 200, response.text
        original = response.json()
        assert original["status"] == outcome, original

    before = await _operation_snapshot(account_id, parent_id)
    assert before["state"] == outcome
    assert before["event_spool_complete"]
    spool_before = await service._durable_bridge.get_operation_events(operation_id=before["operation_id"])
    assert sum(upstream.accepted_operations for upstream in upstreams) == 1
    assert sum(len(upstream.sent_text) for upstream in upstreams) == 2
    if stream:
        replay = await _collect_sse_events(async_client, path, json_body=payload, headers=headers)
    else:
        response = await async_client.post(path, json=payload, headers=headers)
        assert response.status_code == 200, response.text
        replay = response.json()
    assert replay == original
    assert await _operation_snapshot(account_id, parent_id) == before
    assert await service._durable_bridge.get_operation_events(operation_id=before["operation_id"]) == spool_before
    assert sum(upstream.accepted_operations for upstream in upstreams) == 1
    assert sum(len(upstream.sent_text) for upstream in upstreams) == 2
