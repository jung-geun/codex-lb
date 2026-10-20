"""Real HTTP bridge + SQLite regressions; only upstream websocket I/O is synthetic.

Recovery requires a matching stored prefix and the entire persisted direct-call
manifest. A new user does not stand in for an assistant completion boundary.
Known turn annotations are preserved, never stripped to manufacture that proof.
"""

from __future__ import annotations

import copy
import json
import socket
import time

import pytest
from sqlalchemy import delete, select

import app.modules.proxy.service as proxy_service
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.db.models import HttpBridgeOperationRecord, HttpBridgeRecoveryAttemptRecord
from app.db.session import SessionLocal
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy.durable_bridge_repository import durable_bridge_api_key_scope
from tests.integration.test_http_responses_bridge import (
    _FakeBridgeUpstreamWebSocket,
    _cleanup_http_bridge_sessions,  # noqa: F401 -- upstream autouse fixture
    _collect_sse_events,
    _collect_sse_events_with_headers,
    _encode_jwt,
    _install_bridge_settings_with_limits,
    _make_auth_json,
)

pytestmark = pytest.mark.integration

_CONSUMERS = [
    pytest.param("/backend-api/codex/responses", True, id="codex-sse"),
    pytest.param("/v1/responses", False, id="openai-json"),
]
_CALL = {
    "type": "function_call",
    "id": "fc_read",
    "status": "completed",
    "call_id": "call_read",
    "name": "Read",
    "arguments": '{"path":"example.txt"}',
    "metadata": {"turn_id": "old-turn"},
    "internal_chat_message_metadata_passthrough": {
        "turn_id": "old-turn",
        "create_time": 1792157779.0360363,
    },
}
_OUTPUT = {"type": "function_call_output", "call_id": "call_read", "output": "full file text"}
_USER = {"role": "user", "content": [{"type": "input_text", "text": "continue"}]}


class _AnchorTransport(_FakeBridgeUpstreamWebSocket):
    def __init__(self, plan, number):
        super().__init__(f"resp_anchor_{number}")
        self.plan = plan

    async def _event(self, payload):
        await self._messages.put(UpstreamWebSocketMessage("text", text=json.dumps(payload)))

    async def send_text(self, text):
        payload = json.loads(text)
        bootstrap = not self.plan["bootstrapped"]
        if bootstrap:
            self.plan["bootstrapped"] = True
            await super().send_text(text)
            created = self._messages.get_nowait()
            terminal = json.loads(self._messages.get_nowait().text)
            calls = [copy.deepcopy(_CALL)]
            if self.plan.get("parallel_call"):
                calls.append({**copy.deepcopy(_CALL), "id": "fc_parallel", "call_id": "call_parallel"})
            terminal["response"]["output"] = calls
            self.plan["parent"] = terminal["response"]["id"]
            self._messages.put_nowait(created)
            for index, call in enumerate(calls):
                await self._event({
                    "type": "response.output_item.added",
                    "response_id": self.plan["parent"],
                    "output_index": index,
                    "item": {**call, "status": "in_progress"},
                })
                await self._event({
                    "type": "response.output_item.done",
                    "response_id": self.plan["parent"],
                    "output_index": index,
                    "item": call,
                })
            await self._event(terminal)
            return
        if payload.get("previous_response_id") == self.plan["parent"] or self.plan["outcome"] == "disconnect":
            if self.plan["outcome"] == "complete":
                await super().send_text(text)
                return
            self.sent_text.append(text)
            if self.plan["outcome"] == "disconnect":
                await self._event({
                    "type": "response.created",
                    "response": {"id": "resp_uncertain_child", "object": "response", "status": "in_progress"},
                })
                await self._event({
                    "type": "response.output_text.delta", "response_id": "resp_uncertain_child", "delta": "partial",
                })
                await self._messages.put(UpstreamWebSocketMessage("close", close_code=1000))
                return
            await self._event({
                "type": "error", "status": 400,
                "error": {
                    "type": "invalid_request_error", "code": "previous_response_not_found",
                    "message": "Previous response not found.", "param": "previous_response_id",
                },
            })
            return
        await super().send_text(text)


async def _import_account(client, name):
    auth = _make_auth_json(name, f"{name}@example.invalid")
    auth["tokens"]["accessToken"] = _encode_jwt({"sub": name, "exp": int(time.time()) + 86400})
    response = await client.post(
        "/api/accounts/import", files={"auth_json": ("auth.json", json.dumps(auth), "application/json")},
    )
    assert response.status_code == 200, response.text
    return response.json()["accountId"]


async def _install(async_client, app_instance, monkeypatch, *, outcome="deny"):
    _install_bridge_settings_with_limits(monkeypatch, enabled=True, instance_id=socket.gethostname())
    account_id = await _import_account(async_client, "anchor-owner")
    plan = {"bootstrapped": False, "outcome": outcome}
    upstreams = []
    owners = []

    async def connect(headers, access_token, account_id_header, **kwargs):
        owners.append(account_id_header)
        upstream = _AnchorTransport(plan, len(upstreams))
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    service = get_proxy_service_for_app(app_instance)
    return account_id, service, plan, upstreams, owners


def _frames(upstreams):
    return [json.loads(text) for upstream in upstreams for text in upstream.sent_text]


async def _success(client, path, stream, payload, headers):
    payload = {**payload, "stream": stream}
    if stream:
        events, response_headers = await _collect_sse_events_with_headers(
            client, path, json_body=payload, headers=headers,
        )
        if response_headers.get("x-codex-turn-state"):
            headers["x-codex-turn-state"] = response_headers["x-codex-turn-state"]
        assert events[-1]["type"] == "response.completed", events
        return events[-1]["response"]
    response = await client.post(path, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    if response.headers.get("x-codex-turn-state"):
        headers["x-codex-turn-state"] = response.headers["x-codex-turn-state"]
    assert response.json()["status"] == "completed", response.text
    return response.json()


async def _snapshot(account_id, parent, *, operation_id=None):
    async with SessionLocal() as session:
        result = await session.execute(select(HttpBridgeOperationRecord.__table__).where(
            HttpBridgeOperationRecord.account_id == account_id,
            (HttpBridgeOperationRecord.operation_id == operation_id)
            if operation_id is not None else (HttpBridgeOperationRecord.parent_response_id == parent),
        ))
        return dict(result.mappings().one())


def _initial():
    return {
        "model": "gpt-5.1", "instructions": "Read the file, then respond.",
        "tools": [{"type": "function", "name": "Read", "parameters": {"type": "object"}}],
        "input": [{"role": "user", "content": "Read example.txt"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stream", _CONSUMERS)
@pytest.mark.parametrize("encrypted_prefix", [False, True], ids=["text", "owner-bound-reasoning"])
async def test_rejected_injected_parent_replays_full_settled_history_once(
    async_client, app_instance, monkeypatch, path, stream, encrypted_prefix,
):
    _, _, plan, upstreams, owners = await _install(async_client, app_instance, monkeypatch)
    headers = {"session_id": "anchor-recovery-thread", "user-agent": "codex_exec/0.153.4"}
    initial = _initial()
    if encrypted_prefix:
        initial["input"].insert(0, {"type": "reasoning", "encrypted_content": "opaque-owner-private-state"})
    await _success(async_client, path, stream, initial, headers)
    # Another available account must not receive either plaintext or encrypted
    # owner state when the original account's successful anchor is rejected.
    await _import_account(async_client, "anchor-other-owner")
    history = [*initial["input"], copy.deepcopy(_CALL), copy.deepcopy(_OUTPUT), copy.deepcopy(_USER)]
    payload = {**initial, "input": history}
    completed = await _success(async_client, path, stream, payload, headers)
    frames = _frames(upstreams)
    rejected = [frame for frame in frames if frame.get("previous_response_id") == plan["parent"]]
    assert len(rejected) <= 1, "a rejected proxy anchor must never be reinjected"
    if stream:
        assert len(rejected) == 1, "exercise the Codex route's injected-anchor rejection"
        assert len(rejected[0]["input"]) < len(history), "exercise the stored-prefix trim"
    replay = frames[-1]
    assert replay.get("previous_response_id") is None
    assert replay["input"] == history, "never send a suffix-only or metadata-stripped fresh replay"
    assert replay["tools"] == initial["tools"]
    assert sum(frame.get("previous_response_id") is None and frame["input"] == history for frame in frames) == 1
    assert len(set(owners)) == 1

    next_history = [*history, *completed["output"], {"role": "user", "content": "one more question"}]
    await _success(async_client, path, stream, {**initial, "input": next_history}, headers)
    later = _frames(upstreams)[len(frames):]
    assert later
    assert all(frame.get("previous_response_id") != plan["parent"] for frame in later)
    assert all(frame.get("previous_response_id") or frame["input"] == next_history for frame in later)
    assert len(set(owners)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stream", _CONSUMERS)
@pytest.mark.parametrize("legacy_missing_journal", [False, True], ids=["journaled", "legacy-no-journal"])
async def test_exact_unknown_duplicate_stays_fenced_but_new_user_is_distinct(
    async_client, app_instance, monkeypatch, path, stream, legacy_missing_journal,
):
    account_id, service, plan, upstreams, _ = await _install(
        async_client, app_instance, monkeypatch, outcome="disconnect",
    )
    headers = {"session_id": "anchor-uncertain-thread"}
    initial = _initial()
    await _success(async_client, path, stream, initial, headers)
    history = [*initial["input"], copy.deepcopy(_CALL), copy.deepcopy(_OUTPUT)]
    uncertain = {**initial, "input": history, "stream": stream}
    if stream:
        events = await _collect_sse_events(async_client, path, json_body=uncertain, headers=headers)
        assert any(event["type"] == "response.created" for event in events)
        assert events[-1]["type"] == "response.failed", events
    else:
        response = await async_client.post(path, json=uncertain, headers=headers)
        assert response.status_code == 502, response.text
    before = await _snapshot(account_id, plan["parent"])
    assert before["state"] == "acknowledged"
    assert before["response_id"] == "resp_uncertain_child"
    assert not before["event_spool_complete"]
    spool = await service._durable_bridge.get_operation_events(operation_id=before["operation_id"])
    scoped = await service._durable_bridge.get_operation_by_response_id(
        response_id=before["response_id"], api_key_scope=durable_bridge_api_key_scope(None),
    )
    assert scoped is not None and scoped.operation_id == before["operation_id"]
    assert await service._durable_bridge.get_operation_by_response_id(
        response_id=before["response_id"], api_key_scope=durable_bridge_api_key_scope("unrelated-api-key"),
    ) is None
    async with SessionLocal() as session:
        checkpoint_fingerprint = await session.scalar(select(HttpBridgeRecoveryAttemptRecord.request_fingerprint).where(
            HttpBridgeRecoveryAttemptRecord.response_id == before["response_id"],
        ))
    assert checkpoint_fingerprint is not None
    scoped_history = await service._durable_bridge.get_recovery_attempts_by_fingerprint(
        request_fingerprint=checkpoint_fingerprint, api_key_scope=durable_bridge_api_key_scope(None),
    )
    assert any(attempt.response_id == before["response_id"] for attempt in scoped_history)
    assert await service._durable_bridge.get_recovery_attempts_by_fingerprint(
        request_fingerprint=checkpoint_fingerprint, api_key_scope=durable_bridge_api_key_scope("unrelated-api-key"),
    ) == []
    assert await service._durable_bridge.get_operations_by_parent_response_id(
        parent_response_id=plan["parent"], account_id=account_id,
        api_key_scope=durable_bridge_api_key_scope("unrelated-api-key"),
    ) == []
    dispatch_count = len(_frames(upstreams))
    for _ in range(2):
        response = await async_client.post(path, json=uncertain, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "upstream_operation_status_unknown"
        assert len(_frames(upstreams)) == dispatch_count
        assert await _snapshot(account_id, plan["parent"]) == before
    if legacy_missing_journal:
        # Emulate the pre-fix metadata refusal: the acknowledged immutable
        # operation exists, but its original full-body checkpoint never did.
        async with SessionLocal() as session:
            deleted = await session.execute(delete(HttpBridgeRecoveryAttemptRecord).where(
                HttpBridgeRecoveryAttemptRecord.session_id == before["session_id"],
                HttpBridgeRecoveryAttemptRecord.response_id == before["response_id"],
            ))
            assert deleted.rowcount == 1
            await session.commit()
        assert await _snapshot(account_id, plan["parent"]) == before
    plan["outcome"] = "deny"
    new_history = [*history, copy.deepcopy(_USER)]
    completed = await _success(async_client, path, stream, {**initial, "input": new_history}, headers)
    assert await _snapshot(account_id, plan["parent"], operation_id=before["operation_id"]) == before
    assert await service._durable_bridge.get_operation_events(operation_id=before["operation_id"]) == spool
    assert _frames(upstreams)[-1].get("previous_response_id") is None
    assert _frames(upstreams)[-1]["input"] == new_history
    later_history = [*new_history, *completed["output"], {"role": "user", "content": "next distinct question"}]
    await _success(async_client, path, stream, {**initial, "input": later_history}, headers)
    dispatch_count = len(_frames(upstreams))
    # The current durable prefix/anchor now describes two later turns. Losing
    # the old parent must not turn its accepted request into dispatchable work.
    for _ in range(2):
        response = await async_client.post(path, json=uncertain, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "upstream_operation_status_unknown"
        assert len(_frames(upstreams)) == dispatch_count
        assert await _snapshot(account_id, plan["parent"], operation_id=before["operation_id"]) == before
        assert await service._durable_bridge.get_operation_events(operation_id=before["operation_id"]) == spool
    # Negative control: a distinct, newly summarized full context can be shorter
    # than the latest stored prefix without being the old unknown operation.
    shortened = [
        {"role": "user", "content": "Compacted summary: the example file has been read."},
        {"role": "user", "content": "A genuinely new question using the summary."},
    ]
    await _success(async_client, path, stream, {**initial, "input": shortened}, headers)
    assert _frames(upstreams)[-1]["input"] == shortened
    assert await _snapshot(account_id, plan["parent"], operation_id=before["operation_id"]) == before
    assert await service._durable_bridge.get_operation_events(operation_id=before["operation_id"]) == spool


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stream", _CONSUMERS)
@pytest.mark.parametrize("unsafe", [
    "missing-output", "unknown-metadata", "invalid-create-time", "unknown-call-field",
    "compaction", "explicit-anchor", "prefix-mismatch", "missing-parallel-call",
])
async def test_unsafe_history_never_falls_back_to_unanchored_suffix(
    async_client, app_instance, monkeypatch, path, stream, unsafe,
):
    _, _, plan, upstreams, _ = await _install(async_client, app_instance, monkeypatch)
    headers = {"session_id": "anchor-unsafe-thread"}
    initial = _initial()
    if unsafe == "missing-parallel-call":
        plan["parallel_call"] = True
    await _success(async_client, path, stream, initial, headers)
    call = copy.deepcopy(_CALL)
    history = [*initial["input"], call, copy.deepcopy(_OUTPUT), copy.deepcopy(_USER)]
    payload = {**initial, "input": history, "stream": stream}
    if not stream:
        # Generic JSON deliberately preserves complete input without injecting
        # a parent. Supply an explicit contract to exercise its refusal path.
        payload["previous_response_id"] = plan["parent"]
    if unsafe == "missing-output":
        history.pop(-2)
    elif unsafe == "unknown-metadata":
        call["metadata"]["private_state"] = "not-harmless-turn-metadata"
    elif unsafe == "invalid-create-time":
        call["internal_chat_message_metadata_passthrough"]["create_time"] = "not-numeric"
    elif unsafe == "unknown-call-field":
        call["namespace"] = "owner-private"
    elif unsafe == "compaction":
        history.insert(-1, {"type": "compaction", "encrypted_content": "opaque"})
    elif unsafe == "explicit-anchor":
        payload["previous_response_id"] = plan["parent"]
    elif unsafe == "prefix-mismatch":
        # Supply an explicit anchor to keep the malformed history owner-bound;
        # without one this is legitimately a separate client conversation.
        history[0] = {"role": "user", "content": "different prefix"}
        payload["previous_response_id"] = plan["parent"]
    response = await async_client.post(path, json=payload, headers=headers)
    assert response.status_code >= 400, response.text
    frames = _frames(upstreams)[1:]
    assert frames, "exercise an upstream rejection, not just request validation"
    assert all(frame.get("previous_response_id") == plan["parent"] for frame in frames)
    if payload.get("previous_response_id") is None:
        assert len(frames) == 1, "the rejected proxy parent must not be dispatched twice"
    else:
        # Explicit caller anchors keep the existing bounded owner reattach;
        # neither attempt may silently replace that contract with fresh input.
        assert len(frames) <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stream", _CONSUMERS)
async def test_terminal_full_history_retry_after_advance_replays_original_spool(
    async_client, app_instance, monkeypatch, path, stream,
):
    account_id, service, plan, upstreams, _ = await _install(
        async_client, app_instance, monkeypatch, outcome="complete",
    )
    headers = {"session_id": "anchor-terminal-thread"}
    initial = _initial()
    await _success(async_client, path, stream, initial, headers)
    history = [*initial["input"], copy.deepcopy(_CALL), copy.deepcopy(_OUTPUT)]
    payload = {**initial, "input": history}
    completed = await _success(async_client, path, stream, payload, headers)
    before = await _snapshot(account_id, plan["parent"])
    assert before["state"] == "completed" and before["event_spool_complete"]
    spool = await service._durable_bridge.get_operation_events(operation_id=before["operation_id"])
    later_history = [*history, *completed["output"], copy.deepcopy(_USER)]
    await _success(async_client, path, stream, {**initial, "input": later_history}, headers)
    dispatch_count = len(_frames(upstreams))
    replayed = await _success(async_client, path, stream, payload, headers)
    assert replayed == completed
    assert len(_frames(upstreams)) == dispatch_count
    assert await _snapshot(account_id, plan["parent"], operation_id=before["operation_id"]) == before
    assert await service._durable_bridge.get_operation_events(operation_id=before["operation_id"]) == spool


@pytest.mark.asyncio
async def test_new_manifest_followup_does_not_bypass_newer_source_circuit(
    async_client, app_instance, monkeypatch,
):
    _, service, plan, upstreams, _ = await _install(async_client, app_instance, monkeypatch)
    headers = {"session_id": "anchor-circuit-race", "user-agent": "codex_exec/0.153.4"}
    initial = _initial()
    await _success(async_client, "/backend-api/codex/responses", True, initial, headers)
    original_reset = service._reset_http_bridge_session_after_local_terminal_error
    advanced = False

    async def reset_then_peer_advances(session, *args, **kwargs):
        nonlocal advanced
        await original_reset(session, *args, **kwargs)
        if advanced:
            return
        advanced = True
        # Persist an independent peer generation after recovery captured its
        # source generation but before the replacement dispatch can claim it.
        now = time.time()
        await service._durable_bridge.persist_retry_circuit(
            session_key_kind=session.key.affinity_kind,
            session_key_value=session.key.affinity_key,
            api_key_id=session.key.api_key_id,
            consecutive_failures=2, cooldown_until_epoch=now + 60,
            last_detail="upstream_request_timeout", updated_at_epoch=now + 1,
        )

    monkeypatch.setattr(service, "_reset_http_bridge_session_after_local_terminal_error", reset_then_peer_advances)
    history = [*initial["input"], copy.deepcopy(_CALL), copy.deepcopy(_OUTPUT), copy.deepcopy(_USER)]
    response = await async_client.post(
        "/backend-api/codex/responses", json={**initial, "input": history, "stream": True}, headers=headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "upstream_request_timeout"
    assert advanced
    assert len(_frames(upstreams)) == 2, "bootstrap and rejected anchor only; never dispatch the blocked replacement"
    assert _frames(upstreams)[-1]["previous_response_id"] == plan["parent"]
