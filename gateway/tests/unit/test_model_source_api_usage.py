from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import ModelSource, ModelSourceModel
from app.modules.model_sources.api_usage import api_headroom_ratio, api_rate_limit_available, parse_api_rate_headers
from app.modules.model_sources.providers import deepseek
from app.modules.model_sources.providers.base import ProviderError

NOW = datetime(2030, 1, 2, 12)


def source(provider="openai", **headers):
    native = "native-model"
    return ModelSource(
        id="api", provider=provider, billing_mode="api_metered", auth_type="api_key",
        models=[ModelSourceModel(model=provider + "/" + native, upstream_model=native)],
        api_rate_limits=parse_api_rate_headers(provider, headers, native, now=NOW),
    )


def test_header_durations_and_independent_project_limits_control_capacity():
    row = source(**{
        "X-RateLimit-Limit-Tokens": "100000", "X-RateLimit-Remaining-Tokens": "80000",
        "X-RateLimit-Reset-Tokens": "1m500ms", "X-RateLimit-Limit-Project-Tokens": "20000",
        "X-RateLimit-Remaining-Project-Tokens": "0", "X-RateLimit-Reset-Project-Tokens": "2s",
        "X-RateLimit-Limit-Requests": "60", "X-RateLimit-Remaining-Requests": "59",
        "X-RateLimit-Reset-Requests": "1s",
    })
    total, project = row.api_rate_limits
    assert total.tokens_reset_at == NOW + timedelta(seconds=60.5)
    assert project.tokens_remaining == 0 and project.token_limit == 20000
    assert not api_rate_limit_available(row, "openai/native-model", now=NOW)
    assert api_headroom_ratio(row, "native-model", now=NOW) == 0
    # Expiry retires the historical rate signal, not an invented new counter.
    assert api_rate_limit_available(row, "native-model", now=NOW + timedelta(seconds=3))
    assert api_headroom_ratio(row, "native-model", now=NOW + timedelta(seconds=3)) == .8
    assert project.tokens_remaining == 0


def test_anthropic_input_output_limits_remain_separate_and_model_scoped():
    row = source("anthropic", **{
        "anthropic-ratelimit-input-tokens-limit": "50000", "anthropic-ratelimit-input-tokens-remaining": "40000",
        "anthropic-ratelimit-input-tokens-reset": "2030-01-02T12:00:30Z",
        "anthropic-ratelimit-output-tokens-limit": "10000", "anthropic-ratelimit-output-tokens-remaining": "0",
        "anthropic-ratelimit-output-tokens-reset": "2030-01-02T12:01:00Z",
    })
    assert [limit.token_kind for limit in row.api_rate_limits] == ["input_tokens", "output_tokens"]
    assert not api_rate_limit_available(row, "native-model", now=NOW)
    assert api_rate_limit_available(row, "unrelated-model", now=NOW)
    assert api_headroom_ratio(row, "native-model", now=NOW) == 0
    assert api_rate_limit_available(row, "native-model", now=NOW + timedelta(minutes=6))


@pytest.mark.parametrize("remaining", ["NaN", "inf", "-1", "secret-value", "1.2", "9223372036854775808", "２"])
def test_malformed_rate_values_are_unknown_not_zero_or_infinite_capacity(remaining):
    row = source(**{"x-ratelimit-remaining-tokens": remaining, "authorization": "private-token"})
    assert row.api_rate_limits == []
    assert api_rate_limit_available(row, "native-model", now=NOW)
    assert api_headroom_ratio(row, "native-model", now=NOW) == 1


def test_fresh_request_zero_without_reset_blocks_only_the_observed_model():
    row = source(**{"x-ratelimit-remaining-requests": "0"})
    assert not api_rate_limit_available(row, "native-model", now=NOW)
    assert api_rate_limit_available(row, "other", now=NOW)
    assert api_rate_limit_available(row, "native-model", now=NOW + timedelta(minutes=6))
    assert api_rate_limit_available(row, "native-model", now=NOW - timedelta(seconds=31))
    row.api_balance_is_available = False
    assert not api_rate_limit_available(row, "native-model", now=NOW + timedelta(days=1))


def test_native_balance_keeps_currency_decimal_precision_and_funding_state():
    snapshot = deepseek.parse_snapshot({
        "is_available": False, "balance_infos": [{"currency": "USD", "total_balance": "0.000000000000000001",
        "granted_balance": "0", "topped_up_balance": "0.000000000000000001"}],
    })
    assert snapshot.balance_is_available is False
    assert snapshot.balances == [{"currency": "USD", "total_balance": "0.000000000000000001",
        "granted_balance": "0", "topped_up_balance": "0.000000000000000001"}]
    assert snapshot.windows == []


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "private-secret", True, 2])
def test_invalid_native_amount_is_not_a_reported_balance(amount):
    with pytest.raises(ProviderError, match="invalid balance amount"):
        deepseek.parse_snapshot({"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": amount}]})


@pytest.mark.parametrize("shape", ["chat", "responses"])
@pytest.mark.parametrize("reasoning,expected", [(3, 3), (0, 0), (None, None), (True, None), (-1, None), (11, None)])
def test_reported_reasoning_remains_an_output_subset_and_unknown_is_not_zero(shape, reasoning, expected):
    from app.modules.model_sources.forwarding import _usage_from_chat_payload, _usage_from_responses_payload

    if shape == "chat":
        parsed = _usage_from_chat_payload({"usage": {
            "prompt_tokens": 100, "completion_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        }})
    else:
        parsed = _usage_from_responses_payload({"usage": {
            "input_tokens": 100, "output_tokens": 10,
            "output_tokens_details": {"reasoning_tokens": reasoning},
        }})
    assert parsed is not None
    assert parsed.input_tokens + parsed.output_tokens == 110
    assert parsed.reasoning_tokens == expected
