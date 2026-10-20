"""Behavior regressions against the digest-pinned upstream test harness."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.modules.proxy.api as proxy_api
import app.modules.proxy.service as proxy_service
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.core.clock import REAL_CLOCK
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy.load_balancer import AccountSelection
from tests.integration.test_http_responses_bridge import (
    _FakeBridgeUpstreamWebSocket,
    _cleanup_http_bridge_sessions,  # noqa: F401 -- upstream autouse fixture
    _collect_sse_events,
    _get_account,
    _import_account,
    _make_auth_json,
    _install_bridge_settings,
)
from tests.integration.test_proxy_websocket_responses import _websocket_settings

pytestmark = pytest.mark.integration


class OffsetClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.offset = 0.0

    def monotonic(self) -> float:
        return self.now + self.offset

    def time(self) -> float:
        return REAL_CLOCK.time()


class LifetimeUpstream(_FakeBridgeUpstreamWebSocket):
    """A short turn crosses the upstream hour limit on a nearly-expired socket."""

    def __init__(self, clock: OffsetClock, number: int, *, connection_local_anchors: bool = False) -> None:
        super().__init__(f"resp_lifetime_{number}")
        self.clock = clock
        self.connected_at = clock.monotonic()
        self.connection_local_anchors = connection_local_anchors
        self.accepted_turns = 0

    async def send_text(self, text: str) -> None:
        payload = json.loads(text)
        anchor = payload.get("previous_response_id")
        if self.connection_local_anchors and anchor and not anchor.startswith(self.response_id_prefix):
            self.sent_text.append(text)
            await self._messages.put(
                UpstreamWebSocketMessage(
                    "text",
                    text=json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "code": "previous_response_not_found",
                                "message": "Previous response was not found; retry without previous_response_id.",
                            },
                            "status": 400,
                        }
                    ),
                )
            )
            return
        self.accepted_turns += 1
        if self.clock.monotonic() - self.connected_at >= 3400.0:
            self.sent_text.append(text)
            await self._messages.put(
                UpstreamWebSocketMessage(
                    "text",
                    text=json.dumps(
                        {
                            "type": "response.created",
                            "response": {"id": "resp_expiring", "status": "in_progress"},
                        }
                    ),
                )
            )
            await self._messages.put(UpstreamWebSocketMessage("close", close_code=1000))
            return
        await super().send_text(text)


def receive_terminal(websocket) -> dict:
    for _ in range(30):
        event = json.loads(websocket.receive_text())
        if event.get("type") in {"response.completed", "response.failed", "error"}:
            return event
    raise AssertionError("response had no terminal event")


def import_native_account(client, account_id: str):
    auth = _make_auth_json(account_id, f"{account_id}@example.invalid")
    response = client.post(
        "/api/accounts/import",
        files={"auth_json": ("auth.json", json.dumps(auth), "application/json")},
    )
    assert response.status_code == 200, response.text
    return client.portal.call(_get_account, response.json()["accountId"])


@pytest.mark.parametrize("age,local_anchors", [(2999.999, False), (3000.0, False), (3461.0, False), (3461.0, True)])
@pytest.mark.asyncio
async def test_http_turn_rotates_aged_socket_without_losing_continuation(
    async_client, app_instance, monkeypatch, age: float, local_anchors: bool
) -> None:
    _install_bridge_settings(monkeypatch, enabled=True)
    account_id = await _import_account(async_client, "lifetime-http", "lifetime-http@example.invalid")
    account = await _get_account(account_id)
    service = get_proxy_service_for_app(app_instance)
    clock = OffsetClock()
    monkeypatch.setattr(service, "_clock", clock)
    upstreams: list[LifetimeUpstream] = []

    async def select_account(self, *args, **kwargs):
        return AccountSelection(account=account, error_message=None, error_code=None)

    async def ensure_fresh(self, target, **kwargs):
        return target

    async def connect(*args, **kwargs):
        upstream = LifetimeUpstream(clock, len(upstreams), connection_local_anchors=local_anchors)
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(proxy_service.ProxyService, "_select_account_with_budget", select_account)
    monkeypatch.setattr(proxy_service.ProxyService, "_ensure_fresh_with_budget", ensure_fresh)
    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    payload = {
        "model": "gpt-5.1",
        "instructions": "Return exactly OK.",
        "input": [{"role": "user", "content": "first"}],
        "stream": True,
    }
    headers = {"session_id": "lifetime-http-thread"}
    first = await _collect_sse_events(async_client, "/backend-api/codex/responses", json_body=payload, headers=headers)
    assert first[-1]["type"] == "response.completed"
    parent_id = first[-1]["response"]["id"]
    next_input = (
        [*payload["input"], *first[-1]["response"]["output"], {"role": "user", "content": "next"}]
        if local_anchors
        else "next"
    )
    clock.offset = age
    # The connection was busy throughout the simulated interval, not idle.
    for session in service._http_bridge_sessions.values():
        session.last_used_at = clock.monotonic()
    second = await asyncio.wait_for(
        _collect_sse_events(
            async_client,
            "/backend-api/codex/responses",
            json_body={**payload, "input": next_input, "previous_response_id": parent_id},
            headers=headers,
        ),
        timeout=5,
    )
    assert second[-1]["type"] == "response.completed", second
    rotated = age >= 3000.0
    if rotated:
        assert len(upstreams) >= 2
    else:
        assert len(upstreams) == 1
    completed_upstream = upstreams[-1]
    connection_count = len(upstreams)
    assert second[-1]["response"]["id"].startswith(completed_upstream.response_id_prefix + "_")
    assert sum(upstream.accepted_turns for upstream in upstreams) == 2
    if rotated:
        assert upstreams[0].closed
        # A successful reconnect resets the age rather than rotating every turn.
        third = await _collect_sse_events(
            async_client,
            "/backend-api/codex/responses",
            json_body={**payload, "input": "third", "previous_response_id": second[-1]["response"]["id"]},
            headers=headers,
        )
        assert third[-1]["type"] == "response.completed"
        assert third[-1]["response"]["id"].startswith(completed_upstream.response_id_prefix + "_")
        assert len(upstreams) == connection_count


@pytest.mark.parametrize("age,local_anchors", [(2999.999, False), (3000.0, False), (3461.0, False), (3461.0, True)])
def test_native_turn_rotates_aged_socket_without_replaying_accepted_work(
    app_instance, monkeypatch, age: float, local_anchors: bool
) -> None:
    service = get_proxy_service_for_app(app_instance)
    clock = OffsetClock()
    monkeypatch.setattr(service, "_clock", clock)
    upstreams: list[LifetimeUpstream] = []

    class SettingsCache:
        async def get(self):
            return _websocket_settings()

    async def allow_firewall(websocket):
        return None

    async def allow_api_key(authorization, *, request=None):
        return None

    async def connect(self, headers, **kwargs):
        upstream = LifetimeUpstream(clock, len(upstreams), connection_local_anchors=local_anchors)
        upstreams.append(upstream)
        return account, upstream

    monkeypatch.setattr(proxy_api, "_websocket_firewall_denial_response", allow_firewall)
    monkeypatch.setattr(proxy_api, "validate_proxy_api_key_authorization", allow_api_key)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: SettingsCache())
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", connect)
    payload = {
        "type": "response.create",
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": "first"}],
        "stream": True,
    }
    with TestClient(app_instance, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        account = import_native_account(client, "lifetime-native-account")
        with client.websocket_connect(
            "/backend-api/codex/responses", headers={"session_id": "lifetime-native-thread"}
        ) as ws:
            ws.send_json(payload)
            first = receive_terminal(ws)
            assert first["type"] == "response.completed", first
            next_input = (
                [*payload["input"], *first["response"]["output"], {"role": "user", "content": "next"}]
                if local_anchors
                else "next"
            )
            clock.offset = age
            ws.send_json({**payload, "input": next_input, "previous_response_id": first["response"]["id"]})
            second = receive_terminal(ws)
            assert second["type"] == "response.completed", second
            rotated = age >= 3000.0
            if rotated:
                assert len(upstreams) >= 2
            else:
                assert len(upstreams) == 1
            completed_upstream = upstreams[-1]
            connection_count = len(upstreams)
            assert second["response"]["id"].startswith(completed_upstream.response_id_prefix + "_")
            assert sum(upstream.accepted_turns for upstream in upstreams) == 2
            if rotated:
                assert upstreams[0].closed
                ws.send_json({**payload, "input": "third", "previous_response_id": second["response"]["id"]})
                third = receive_terminal(ws)
                assert third["type"] == "response.completed", third
                assert third["response"]["id"].startswith(completed_upstream.response_id_prefix + "_")
                assert len(upstreams) == connection_count


class HoldingUpstream(_FakeBridgeUpstreamWebSocket):
    def __init__(self) -> None:
        super().__init__("resp_holding")
        self.accepted_turns = 0

    async def send_text(self, text: str) -> None:
        self.accepted_turns += 1
        if self.accepted_turns != 1:
            await super().send_text(text)
            return
        self.sent_text.append(text)
        await self._messages.put(
            UpstreamWebSocketMessage(
                "text",
                text=json.dumps(
                    {
                        "type": "response.created",
                        "response": {"id": "resp_holding_1", "object": "response", "status": "in_progress"},
                    }
                ),
            )
        )

    async def finish_first(self) -> None:
        await self._messages.put(
            UpstreamWebSocketMessage(
                "text",
                text=json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_holding_1",
                            "object": "response",
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": "OK"}],
                                }
                            ],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        },
                    }
                ),
            )
        )


@pytest.mark.asyncio
async def test_http_lifetime_rotation_preserves_multiple_accepted_turns(
    async_client, app_instance, monkeypatch
) -> None:
    _install_bridge_settings(monkeypatch, enabled=True)
    account_id = await _import_account(async_client, "lifetime-active", "lifetime-active@example.invalid")
    account = await _get_account(account_id)
    service = get_proxy_service_for_app(app_instance)
    upstreams: list[HoldingUpstream] = []
    created = asyncio.Event()
    original_process = service._process_http_bridge_upstream_text

    async def observe_created(session, text, **kwargs):
        await original_process(session, text, **kwargs)
        event = json.loads(text)
        if event.get("type") == "response.created" and event["response"]["id"] == "resp_holding_1":
            created.set()

    async def select_account(self, *args, **kwargs):
        return AccountSelection(account=account, error_message=None, error_code=None)

    async def ensure_fresh(self, target, **kwargs):
        return target

    async def connect(*args, **kwargs):
        upstream = HoldingUpstream()
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(service, "_process_http_bridge_upstream_text", observe_created)
    monkeypatch.setattr(proxy_service.ProxyService, "_select_account_with_budget", select_account)
    monkeypatch.setattr(proxy_service.ProxyService, "_ensure_fresh_with_budget", ensure_fresh)
    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    payload = {"model": "gpt-5.1", "input": "first", "stream": True}
    headers = {"session_id": "lifetime-active-thread"}
    first_task = asyncio.create_task(
        _collect_sse_events(async_client, "/backend-api/codex/responses", json_body=payload, headers=headers)
    )
    try:
        await asyncio.wait_for(created.wait(), timeout=5)
        for session in service._http_bridge_sessions.values():
            session.upstream_control.connected_at = REAL_CLOCK.monotonic() - 3461.0
        second = await asyncio.wait_for(
            _collect_sse_events(
                async_client,
                "/backend-api/codex/responses",
                json_body={**payload, "input": "next", "previous_response_id": "resp_holding_1"},
                headers=headers,
            ),
            timeout=5,
        )
        assert second[-1]["type"] == "response.completed", second
        assert second[-1]["response"]["id"] == "resp_holding_2"
        assert len(upstreams) == 1
        assert not upstreams[0].closed
        await upstreams[0].finish_first()
        first = await asyncio.wait_for(first_task, timeout=5)
        assert first[-1]["type"] == "response.completed", first
        assert first[-1]["response"]["id"] == "resp_holding_1"
        assert upstreams[0].accepted_turns == 2
    finally:
        if not first_task.done():
            first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)


def test_native_lifetime_rotation_preserves_multiple_accepted_turns(app_instance, monkeypatch) -> None:
    from app.modules.proxy._service.websocket import mixin as websocket_mixin

    controls = []
    upstreams: list[HoldingUpstream] = []
    control_type = websocket_mixin._WebSocketUpstreamControl

    def capture_control(**kwargs):
        control = control_type(**kwargs)
        controls.append(control)
        return control

    class SettingsCache:
        async def get(self):
            return _websocket_settings()

    async def allow_firewall(websocket):
        return None

    async def allow_api_key(authorization, *, request=None):
        return None

    async def connect(self, headers, **kwargs):
        upstream = HoldingUpstream()
        upstreams.append(upstream)
        return account, upstream

    monkeypatch.setattr(websocket_mixin, "_WebSocketUpstreamControl", capture_control)
    monkeypatch.setattr(proxy_api, "_websocket_firewall_denial_response", allow_firewall)
    monkeypatch.setattr(proxy_api, "validate_proxy_api_key_authorization", allow_api_key)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: SettingsCache())
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", connect)
    payload = {"type": "response.create", "model": "gpt-5.4", "input": "first", "stream": True}
    with TestClient(app_instance, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        account = import_native_account(client, "lifetime-native-active")
        with client.websocket_connect("/backend-api/codex/responses") as ws:
            ws.send_json(payload)
            assert json.loads(ws.receive_text())["type"] == "response.created"
            controls[0].connected_at = REAL_CLOCK.monotonic() - 3461.0
            ws.send_json({**payload, "input": "next", "previous_response_id": "resp_holding_1"})
            second = receive_terminal(ws)
            assert second["type"] == "response.completed", second
            assert second["response"]["id"] == "resp_holding_2"
            assert len(upstreams) == 1
            assert not upstreams[0].closed
            client.portal.call(upstreams[0].finish_first)
            first = receive_terminal(ws)
            assert first["type"] == "response.completed", first
            assert first["response"]["id"] == "resp_holding_1"
            assert upstreams[0].accepted_turns == 2
