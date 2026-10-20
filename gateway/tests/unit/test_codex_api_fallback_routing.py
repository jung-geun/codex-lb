"""Isolated pre-dispatch routing: fallback is never an inference retry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import app.modules.proxy._service.websocket.mixin as websocket
import app.modules.proxy.api as proxy_api
import app.modules.proxy.source_fallback as fallback
from app.core.openai.requests import ResponsesRequest

pytestmark = pytest.mark.unit
MODEL = "gpt-exact-test"


@pytest.fixture
def routing(monkeypatch):
    registry = SimpleNamespace(get_models_with_fallback=lambda: {MODEL: object()})
    monkeypatch.setattr(fallback, "get_model_registry", lambda: registry)
    settings = object()
    monkeypatch.setattr(fallback, "get_settings_cache", lambda: SimpleNamespace(get=AsyncMock(return_value=settings)))
    monkeypatch.setattr(fallback, "effective_account_concurrency_caps", lambda value: "caps")
    snapshot = AsyncMock(return_value=1)
    service = SimpleNamespace(
        _load_balancer=SimpleNamespace(eligible_account_routing_rank=snapshot),
        _resolve_websocket_previous_response_owner=AsyncMock(return_value=None),
    )
    return service, snapshot, settings


@pytest.mark.asyncio
@pytest.mark.parametrize("rank,expected", [(0, 0), (1, 1), (2, 2), (None, 3)])
async def test_fresh_turn_uses_actual_scoped_capacity_snapshot(routing, rank, expected):
    service, snapshot, settings = routing
    snapshot.return_value = rank
    key = SimpleNamespace(
        billing_policy="any", assigned_account_ids=["assigned"], account_assignment_scope_enabled=True,
    )
    assert await fallback.codex_api_fallback_tier(service, MODEL, key, service_tier="default") == expected
    snapshot.assert_awaited_once_with(
        model=MODEL, service_tier="default", account_ids=["assigned"],
        concurrency_caps="caps", dashboard_settings=settings,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("constraints", [
    {"previous_response_id": "resp_account_owned"},
    {"required_account_id": "owner"},
    {"source_route_excluded": True},
    {"require_security_work_authorized": True},
])
async def test_native_constraints_never_enable_paid_fallback(routing, constraints):
    service, snapshot, _ = routing
    snapshot.return_value = None
    assert await fallback.codex_api_fallback_tier(service, MODEL, None, **constraints) is None
    snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscription_only_never_enables_paid_fallback(routing):
    service, snapshot, _ = routing
    assert await fallback.codex_api_fallback_tier(
        service, MODEL, SimpleNamespace(billing_policy="subscription_only"),
    ) is None
    snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [True, False])
async def test_http_real_selection_seam_passes_native_policy_to_paid_catalog(monkeypatch, routing, eligible):
    service, snapshot, _ = routing
    snapshot.return_value = 1 if eligible else None
    paid = object()

    async def select(model, key, **kwargs):
        assert model == MODEL
        if kwargs.get("codex_fallback_tier") == 3:
            return paid, "openai/" + MODEL
        return None

    selector = AsyncMock(side_effect=select)
    monkeypatch.setattr(proxy_api, "_select_responses_model_source", selector)
    payload = ResponsesRequest(model=MODEL, instructions="", input="hello", stream=True)
    request = Request({"type": "http", "headers": []})
    result, suppressed = await proxy_api._select_responses_model_source_with_continuity(
        request, payload, SimpleNamespace(service=service), None, require_streaming=True,
    )
    assert result == (None if eligible else (paid, "openai/" + MODEL))
    assert not suppressed
    assert selector.await_count == 2
    assert selector.await_args.kwargs["codex_fallback_tier"] == (1 if eligible else 3)
    snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_account_continuation_never_consults_paid_fallback(monkeypatch, routing):
    service, snapshot, _ = routing
    selector = AsyncMock(return_value=None)
    monkeypatch.setattr(proxy_api, "_select_responses_model_source", selector)
    result = await proxy_api._select_responses_model_source_with_continuity(
        Request({"type": "http", "headers": []}),
        ResponsesRequest(model=MODEL, instructions="", input="hello", previous_response_id="resp_owner"),
        SimpleNamespace(service=service), None,
    )
    assert result == (None, False)
    snapshot.assert_not_awaited()
    selector.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [True, False])
async def test_websocket_native_model_can_downgrade_to_exact_http_source(monkeypatch, routing, eligible):
    service, snapshot, _ = routing
    snapshot.return_value = 1 if eligible else None
    monkeypatch.setattr(websocket, "responses_model_is_source_owned", AsyncMock(return_value=False))
    selector = AsyncMock(return_value=None if eligible else (object(), "openai/" + MODEL))
    monkeypatch.setattr(websocket, "select_responses_model_source", selector)
    state = SimpleNamespace(
        api_key=None, model=MODEL, raw_source_model=MODEL, service_tier=None,
        previous_response_id=None, preferred_account_id=None, source_route_excluded=False,
        require_security_work_authorized=False,
    )
    assert await websocket._responses_model_requires_http(service, state, None) is not eligible
    assert selector.await_count == 1
    assert selector.await_args.kwargs["codex_fallback_tier"] == (1 if eligible else 3)


@pytest.mark.asyncio
async def test_websocket_does_not_downgrade_without_exact_api_entry(monkeypatch, routing):
    service, snapshot, _ = routing
    snapshot.return_value = None
    monkeypatch.setattr(websocket, "responses_model_is_source_owned", AsyncMock(return_value=False))
    monkeypatch.setattr(websocket, "select_responses_model_source", AsyncMock(return_value=None))
    state = SimpleNamespace(
        api_key=None, model=MODEL, raw_source_model=MODEL, service_tier=None,
        previous_response_id=None, preferred_account_id=None, source_route_excluded=False,
        require_security_work_authorized=False,
    )
    assert not await websocket._responses_model_requires_http(service, state, None)


@pytest.mark.asyncio
async def test_paid_native_fallback_rejects_unsupported_tools_before_admission(monkeypatch):
    registry = SimpleNamespace(get_models_with_fallback=lambda: {MODEL: object()})
    monkeypatch.setattr(proxy_api, "get_model_registry", lambda: registry)
    monkeypatch.setattr(proxy_api, "source_model_supported_tool_types", lambda source, model: frozenset())
    response = await proxy_api._source_responses_response(
        Request({"type": "http", "headers": [], "method": "POST", "path": "/v1/responses"}),
        ResponsesRequest(model="openai/" + MODEL, instructions="", input="hello", tools=[{"type": "computer"}]),
        source=SimpleNamespace(billing_mode="api_metered"),
        api_key=None,
        rate_limit_headers={},
        pre_normalization_effort=None,
        request_model=MODEL,
    )
    assert response.status_code == 400
    assert b"model_source_feature_unsupported" in response.body


@pytest.mark.asyncio
async def test_catalog_alias_does_not_replace_authorized_request_model_for_limits(monkeypatch):
    from unittest.mock import Mock

    registry = SimpleNamespace(get_models_with_fallback=lambda: {MODEL: object()})
    monkeypatch.setattr(proxy_api, "get_model_registry", lambda: registry)
    monkeypatch.setattr(proxy_api, "source_model_supported_tool_types", lambda source, model: frozenset())
    monkeypatch.setattr(proxy_api, "restore_source_reasoning_effort", lambda *args, **kwargs: None)
    monkeypatch.setattr(proxy_api, "_source_cost_is_unavailable", lambda *args, **kwargs: False)
    claims = SimpleNamespace(release_if_unowned=Mock())
    monkeypatch.setattr(proxy_api, "try_claim_source_admission", lambda source: claims)
    limits = AsyncMock(side_effect=RuntimeError("stop before dispatch"))
    monkeypatch.setattr(proxy_api, "_enforce_request_limits", limits)
    with pytest.raises(RuntimeError, match="stop before dispatch"):
        await proxy_api._source_responses_response(
            Request({"type": "http", "headers": []}),
            ResponsesRequest(model="openai/" + MODEL, instructions="", input="hello"),
            source=SimpleNamespace(billing_mode="api_metered"),
            api_key=None,
            rate_limit_headers={},
            pre_normalization_effort=None,
            request_model=MODEL,
        )
    assert limits.await_args.kwargs["request_model"] == MODEL
    claims.release_if_unowned.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ["turn_state", "conversation"])
async def test_http_account_bound_anchor_never_consults_paid_api(monkeypatch, routing, anchor):
    service, snapshot, _ = routing
    snapshot.return_value = None
    selector = AsyncMock(return_value=None)
    monkeypatch.setattr(proxy_api, "_select_responses_model_source", selector)
    headers = [(b"x-codex-turn-state", b"turn_account_bound")] if anchor == "turn_state" else []
    payload = ResponsesRequest(
        model=MODEL, instructions="", input="hello",
        conversation="conv_account_bound" if anchor == "conversation" else None,
    )
    result = await proxy_api._select_responses_model_source_with_continuity(
        Request({"type": "http", "headers": headers}), payload, SimpleNamespace(service=service), None,
    )
    assert result == (None, False)
    snapshot.assert_not_awaited()
    selector.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", [
    {"response_create_attempt_count": 1}, {"operation_dispatched": True},
    {"response_id": "resp_accepted"}, {"replay_required_account_id": "owner"},
])
async def test_websocket_native_fallback_never_reselects_after_dispatch(monkeypatch, routing, constraint):
    service, snapshot, _ = routing
    snapshot.return_value = None
    monkeypatch.setattr(websocket, "responses_model_is_source_owned", AsyncMock(return_value=False))
    selector = AsyncMock(return_value=(object(), "openai/" + MODEL))
    monkeypatch.setattr(websocket, "select_responses_model_source", selector)
    state = SimpleNamespace(
        api_key=None, model=MODEL, raw_source_model=MODEL, service_tier=None,
        previous_response_id=None, preferred_account_id=None, source_route_excluded=False,
        require_security_work_authorized=False, **constraint,
    )
    assert not await websocket._responses_model_requires_http(service, state, None)
    snapshot.assert_not_awaited()
    selector.assert_not_awaited()


def test_native_http_placeholder_evidence_requires_426_and_exact_authenticated_retry(monkeypatch):
    from collections import OrderedDict

    import app.modules.proxy._service.support as support

    monkeypatch.setattr(support, "_source_http_required", OrderedDict())
    monkeypatch.setattr(support, "_source_http_retry_evidence", OrderedDict())
    key, other_key = SimpleNamespace(id="first-key"), SimpleNamespace(id="second-key")
    original = {"session_id": "session", "thread-id": "thread"}
    turn = "turn_" + "a" * 32
    headers = {**original, "x-codex-turn-state": turn}
    support.mark_source_websocket_http_required(original, key, native_model=MODEL, synthesized_turn_state=turn)
    assert not support.consume_source_http_retry_evidence(headers, key, MODEL)
    assert support.consume_source_websocket_http_required(headers, key)
    for changed_headers, changed_key, changed_model in (
        (headers, other_key, MODEL), ({**headers, "session_id": "other-session"}, key, MODEL),
        ({**headers, "thread-id": "other-thread"}, key, MODEL), (headers, key, "different-model"),
        ({**headers, "x-codex-turn-state": "turn_" + "b" * 32}, key, MODEL),
    ):
        assert not support.consume_source_http_retry_evidence(changed_headers, changed_key, changed_model)
    assert support.consume_source_http_retry_evidence(headers, key, MODEL)
    assert not support.consume_source_http_retry_evidence(headers, key, MODEL)
