from __future__ import annotations

import json

import pytest

from app.db.models import ModelSource, ModelSourceOAuthCredential
from app.modules.model_sources.anthropic import (
    NativeUsageParser,
    native_oauth_payload,
    native_quota_observations,
    native_source_principal,
    native_usage,
    validate_native_completion,
)
from app.modules.model_sources.forwarding import SourceUsageHolder


def _event(kind: str, **payload) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n".encode()


def test_native_usage_keeps_cache_read_and_both_creation_ttls_separate():
    usage = native_usage(
        {
            "model": "claude-actual",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 20},
            },
        }
    )
    assert usage is not None
    assert usage.input_tokens == 161
    assert usage.output_tokens == 7
    assert usage.cached_input_tokens == 100
    assert usage.cache_creation_input_tokens == 50
    assert usage.cache_creation_5m_input_tokens == 30
    assert usage.cache_creation_1h_input_tokens == 20
    assert usage.served_model == "claude-actual"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"usage": {}},
        {"usage": {"input_tokens": 4}},
        {"usage": {"input_tokens": True, "output_tokens": 0}},
        {"usage": {"input_tokens": 1, "output_tokens": -1}},
        {"usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": -3}},
    ],
)
def test_missing_or_invalid_native_usage_is_unknown_not_zero(payload):
    assert native_usage(payload) is None


def test_native_consumer_tracks_usage_through_ping_thinking_signature_and_tool_sequence():
    holder = SourceUsageHolder()
    parser = NativeUsageParser(holder)
    wire = b"".join(
        [
            _event(
                "message_start",
                message={
                    "id": "msg_actual",
                    "model": "claude-actual",
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                        "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 3},
                    },
                },
            ),
            _event("ping"),
            _event("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""}),
            _event("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "reason"}),
            _event("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "signed"}),
            _event("content_block_stop", index=0),
            _event(
                "content_block_start",
                index=1,
                content_block={"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}},
            ),
            _event("content_block_delta", index=1, delta={"type": "input_json_delta", "partial_json": '{"query":"x"}'}),
            _event("content_block_stop", index=1),
            _event("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 17}),
            _event("message_stop"),
        ]
    )
    # Arbitrary transport chunk boundaries include split JSON and SSE delimiters.
    for index in range(0, len(wire), 7):
        parser.feed(wire[index : index + 7])
    parser.finish()
    assert holder.response_id == "msg_actual"
    assert holder.terminal_kind == "completed"
    assert holder.first_content_seen
    assert holder.usage is not None
    assert holder.usage.input_tokens == 18
    assert holder.usage.output_tokens == 17
    assert holder.usage.served_model == "claude-actual"
    assert holder.usage.cache_creation_1h_input_tokens == 3


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_unterminated_message_stop_never_completes_native_operation(suffix):
    holder = SourceUsageHolder()
    parser = NativeUsageParser(holder)
    parser.feed(b'event: message_stop\ndata: {"type":"message_stop"}' + suffix)
    parser.finish()
    assert holder.terminal_kind is None


def test_message_start_usage_is_not_final_usage_for_cancelled_output():
    holder = SourceUsageHolder()
    parser = NativeUsageParser(holder)
    parser.feed(
        _event("message_start", message={"model": "claude-actual", "usage": {"input_tokens": 12, "output_tokens": 0}})
    )
    parser.feed(_event("content_block_delta", delta={"type": "text_delta", "text": "partial"}))
    assert holder.first_content_seen
    assert holder.usage is None
    assert holder.terminal_kind is None


@pytest.mark.parametrize("final_usage", [None, {}, {"output_tokens": 0}, {"output_tokens": 7}])
def test_completed_text_without_final_output_usage_remains_unknown(final_usage):
    holder = SourceUsageHolder()
    parser = NativeUsageParser(holder)
    parser.feed(
        _event("message_start", message={"model": "claude-actual", "usage": {"input_tokens": 12, "output_tokens": 0}})
    )
    parser.feed(_event("content_block_delta", index=0, delta={"type": "text_delta", "text": "complete answer"}))
    parser.feed(
        _event(
            "message_delta", delta={"stop_reason": "end_turn"},
            **({"usage": final_usage} if final_usage is not None else {}),
        )
    )
    parser.feed(_event("message_stop"))
    parser.finish()
    assert holder.terminal_kind == "completed" and holder.first_content_seen
    if not final_usage:
        assert holder.usage is None
    else:
        assert holder.usage is not None
        assert holder.usage.input_tokens == 12
        assert holder.usage.output_tokens == final_usage["output_tokens"]


def test_oauth_account_uuid_rewrite_is_scoped_to_existing_native_metadata_only():
    old_uuid = "11111111-1111-1111-1111-111111111111"
    new_uuid = "22222222-2222-2222-2222-222222222222"
    source = ModelSource(provider="claude", auth_type="claude_oauth")
    source.oauth_credential = ModelSourceOAuthCredential(account_uuid=new_uuid)
    payload = {
        "metadata": {"user_id": json.dumps({"account_uuid": old_uuid, "session_id": "session-A"}), "other": "keep"},
        "messages": [{"role": "user", "content": "account_uuid " + old_uuid}],
        "native_extension": True,
    }
    shaped = native_oauth_payload(source, payload)
    assert json.loads(shaped["metadata"]["user_id"]) == {"account_uuid": new_uuid, "session_id": "session-A"}
    assert shaped["messages"] == payload["messages"]
    assert shaped["native_extension"] is True and shaped["metadata"]["other"] == "keep"
    absent = {"messages": [], "metadata": {"user_id": "opaque-application-user"}}
    assert native_oauth_payload(source, absent) is absent
    source.provider, source.auth_type = "zai", "api_key"
    assert native_oauth_payload(source, payload) is payload


def test_native_unified_headers_require_real_ratio_reset_pairs_and_keep_model_scope():
    observations = native_quota_observations(
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.7",
            "anthropic-ratelimit-unified-5h-reset": "1900000000",
            "anthropic-ratelimit-unified-7d-utilization": "NaN",
            "anthropic-ratelimit-unified-7d-reset": "1900000000",
            "anthropic-ratelimit-unified-7d_oi-utilization": "1.1",
            "anthropic-ratelimit-unified-7d_oi-reset": "1900000000",
        },
        model="claude-actual",
    )
    assert len(observations) == 2
    assert observations[0].window_key == "five_hour" and observations[0].used_ratio == 0.7
    assert observations[0].model is None
    assert observations[1].model == "claude-actual" and observations[1].used_ratio == 1
    assert native_quota_observations({"anthropic-ratelimit-unified-5h-utilization": "0.1"}, model="claude-actual") == []


def test_native_message_delta_retains_final_container_identity():
    holder = SourceUsageHolder()
    parser = NativeUsageParser(holder)
    parser.feed(
        _event(
            "message_delta",
            delta={"stop_reason": "end_turn", "container": {"id": "container-A"}},
            usage={"output_tokens": 1},
        )
    )
    from app.modules.model_sources.anthropic import continuation_token

    assert continuation_token("container", "container-A") in holder.continuation_tokens


def test_native_message_root_changes_invalidate_opaque_owner_identity():
    source = ModelSource(
        provider="custom",
        auth_type="api_key",
        quota_group=None,
        id="source",
        base_url="https://same.example/chat",
        messages_base_url="https://one.example/messages",
    )
    source.oauth_credential = None
    source.api_key_encrypted = b"same-credential"
    first = native_source_principal(source)
    source.messages_base_url = "https://two.example/messages"
    assert native_source_principal(source) != first


def test_native_principal_tracks_effective_provider_and_auth_not_unused_ciphertext():
    source = ModelSource(
        id="source", provider="custom", auth_type="api_key", quota_group=None,
        base_url="https://same.example", api_key_encrypted=b"retained-credential",
    )
    source.oauth_credential = None
    authenticated = native_source_principal(source)
    source.auth_type = "none"
    anonymous = native_source_principal(source)
    assert anonymous != authenticated
    source.api_key_encrypted = b"another-unused-credential"
    assert native_source_principal(source) == anonymous
    source.provider = "anthropic"
    assert native_source_principal(source) != anonymous


@pytest.mark.parametrize(
    "payload,count_tokens",
    [
        ({}, False),
        ({"type": "message", "content": []}, False),
        (
            {
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "model": "claude",
                "content": [],
                "stop_reason": None,
            },
            False,
        ),
        ({}, True),
        ({"input_tokens": True}, True),
        ({"input_tokens": -1}, True),
    ],
)
def test_nonterminal_or_malformed_native_json_is_not_success(payload, count_tokens):
    import aiohttp

    with pytest.raises(aiohttp.ClientPayloadError):
        validate_native_completion(payload, count_tokens=count_tokens)
