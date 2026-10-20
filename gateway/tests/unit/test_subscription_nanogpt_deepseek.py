from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime

import pytest

from app.modules.model_sources.providers import deepseek, nanogpt
from app.modules.model_sources.providers.base import ProviderError

# Synthetic values follow the official schemas linked in each adapter; these
# fixtures are test inputs, never live account evidence or provider defaults.
NANO_USAGE = {
    "active": True,
    "state": "active",
    "allowOverage": False,
    "limits": {"dailyInputTokens": 1000, "weeklyInputTokens": 5000, "dailyImages": 10},
    "dailyInputTokens": {"used": 250, "remaining": 750, "percentUsed": 0.25, "resetAt": 1893542400000},
    "weeklyInputTokens": {"used": 2000, "remaining": 3000, "percentUsed": 0.4, "resetAt": 1894060800000},
    "dailyImages": {"used": 10, "remaining": 0, "percentUsed": 1, "resetAt": 1893542400000},
    "period": {"currentPeriodEnd": "2030-02-01T00:00:00Z"},
    "routing": {
        "recommendedMode": "subscription",
        "subscriptionQuotaAvailable": True,
        "paidSpendPolicyAllowsBalance": False,
    },
}
NANO_CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "native/included",
            "name": "Native included",
            "context_length": 123456,
            "max_output_tokens": 3456,
            "capabilities": {"tool_calling": True, "vision": False},
            "supported_service_tiers": ["flex"],
        },
        {"id": "native/paid", "subscription_eligible": True, "subscription_verified": True},
    ],
}
NANO_COVERAGE = {"object": "list", "data": [{"id": "native/included"}, {"id": "coverage/not-in-catalog"}]}
DEEP_BALANCE = {
    "is_available": True,
    "balance_infos": [
        {"currency": "USD", "total_balance": "3.25", "granted_balance": "1.00", "topped_up_balance": "2.25"},
    ],
}
DEEP_CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "native-deepseek",
            "object": "model",
            "owned_by": "deepseek",
            "name": "Native DeepSeek",
            "context_window": 234567,
            "max_output_tokens": 12345,
            "input_modalities": ["text", "image"],
            "effort": {"supported_levels": ["high"], "default_level": "high"},
        },
        {"id": "deepseek-reasoner", "subscription_eligible": True, "subscription_verified": True},
    ],
}


def install_nano(monkeypatch, *, usage=None, catalog=None, coverage=None):
    payloads = {
        nanogpt.USAGE_URL: NANO_USAGE if usage is None else usage,
        nanogpt.CATALOG_URL: NANO_CATALOG if catalog is None else catalog,
        nanogpt.COVERAGE_URL: NANO_COVERAGE if coverage is None else coverage,
    }
    calls = []

    async def request(method, url, *, headers):
        assert method == "GET"  # No inference, mutation, or management-token probe.
        assert headers == {"Authorization": "Bearer synthetic-nano", "Accept": "application/json"}
        assert url in payloads  # No redirect/fallback origin or credential in URL.
        calls.append(url)
        payload = payloads[url]
        if isinstance(payload, Exception):
            raise payload
        return deepcopy(payload)

    monkeypatch.setattr(nanogpt, "request_json", request)
    return calls


@pytest.mark.asyncio
async def test_nano_requires_native_plan_policy_and_separate_coverage(monkeypatch):
    calls = install_nano(monkeypatch)
    snapshot = await nanogpt.inspect("synthetic-nano")
    assert calls == [
        "https://nano-gpt.com/api/subscription/v1/usage",
        "https://nano-gpt.com/api/v1/models?detailed=true",
        "https://nano-gpt.com/api/subscription/v1/models?detailed=true",
    ]
    assert snapshot.billing_mode == "subscription"
    assert snapshot.entitlement_status == "verified"
    assert snapshot.extra_usage_enabled is False
    assert snapshot.quota_group is None  # No key fingerprint pretending to be a principal.
    assert [model.model for model in snapshot.models] == ["native/included", "native/paid"]
    assert [model.subscription_eligible for model in snapshot.models] == [True, False]
    included, paid = snapshot.models
    assert (included.context_window, included.max_output_tokens) == (123456, 3456)
    assert included.supports_tools is True
    assert included.supports_vision is False
    assert json.loads(included.raw_metadata_json)["supported_service_tiers"] == ["flex"]
    assert paid.context_window is None and paid.max_output_tokens is None
    assert paid.supports_tools is None and paid.supports_vision is None
    assert "subscription_verified" not in paid.raw_metadata_json
    assert "provider_catalog" not in included.raw_metadata_json
    assert [window.window_key for window in snapshot.windows] == ["dailyInputTokens", "weeklyInputTokens"]
    assert snapshot.windows[0].used_ratio == 0.25  # Fraction, not 0.25 percent.
    assert snapshot.windows[0].used_value == 250
    assert snapshot.windows[0].limit_value == 1000
    assert snapshot.windows[0].resets_at == datetime(2030, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", [None, True, 0, "false"])
async def test_nano_unknown_or_allowed_paygo_never_acquires_eligibility(monkeypatch, paid):
    usage = deepcopy(NANO_USAGE)
    usage["routing"]["paidSpendPolicyAllowsBalance"] = paid
    install_nano(monkeypatch, usage=usage)
    snapshot = await nanogpt.inspect("synthetic-nano")
    assert snapshot.entitlement_status != "verified"
    assert all(not model.subscription_eligible for model in snapshot.models)
    assert snapshot.extra_usage_enabled is (True if paid is True else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"active": False},
        {"active": "true"},
        {"state": "inactive"},
        {"state": []},
        {"routing": {"recommendedMode": "subscription"}},
        {"routing": {"paidSpendPolicyAllowsBalance": False}},
        {"routing": {"paidSpendPolicyAllowsBalance": False, "recommendedMode": "unavailable"}},
        {"routing": {"paidSpendPolicyAllowsBalance": False, "recommendedMode": "paygo"}},
        {"subscription_verified": True, "subscriptionType": "premium", "active": None},
    ],
)
async def test_nano_catalog_and_self_assertions_cannot_prove_entitlement(monkeypatch, mutation):
    usage = {**deepcopy(NANO_USAGE), **mutation}
    install_nano(monkeypatch, usage=usage)
    snapshot = await nanogpt.inspect("synthetic-nano")
    assert snapshot.entitlement_status != "verified"
    assert len(snapshot.models) == 2
    assert all(not model.subscription_eligible for model in snapshot.models)


@pytest.mark.asyncio
async def test_nano_allow_overage_false_does_not_prove_paygo_is_disabled(monkeypatch):
    usage = deepcopy(NANO_USAGE)
    del usage["routing"]
    install_nano(monkeypatch, usage=usage)
    snapshot = await nanogpt.inspect("synthetic-nano")
    assert snapshot.extra_usage_enabled is None
    assert snapshot.entitlement_status == "unverified"
    assert not any(model.subscription_eligible for model in snapshot.models)


@pytest.mark.parametrize(
    "bucket",
    [
        None,
        {},
        {"degraded": True, "used": 0, "percentUsed": 0, "resetAt": 1893542400000},
        {"used": None, "percentUsed": None, "resetAt": 1893542400000, "degraded": True},
        {"used": True, "percentUsed": 0, "resetAt": 1893542400000},
        {"used": -1, "percentUsed": -0.1, "resetAt": 1893542400000},
        {"used": 0, "percentUsed": float("nan"), "resetAt": 1893542400000},
        {"used": 0, "percentUsed": 0, "resetAt": None},
    ],
)
def test_nano_degraded_or_missing_counters_are_unknown_not_full_allowance(bucket):
    usage = deepcopy(NANO_USAGE)
    usage["dailyInputTokens"] = bucket
    snapshot = nanogpt.parse_snapshot(usage)
    assert snapshot.entitlement_status == "unverified"
    assert snapshot.windows[0].used_ratio is None
    assert snapshot.windows[0].used_value is None
    assert snapshot.windows[0].limit_value == 1000


@pytest.mark.parametrize("missing", ["quota", "limit"])
def test_nano_missing_required_window_is_not_inferred_unconfigured(missing):
    usage = deepcopy(NANO_USAGE)
    del usage["weeklyInputTokens"]
    if missing == "limit":
        del usage["limits"]["weeklyInputTokens"]
    snapshot = nanogpt.parse_snapshot(usage)
    assert snapshot.entitlement_status == "unverified"


def test_nano_explicit_null_window_and_trial_native_units():
    usage = deepcopy(NANO_USAGE)
    for name in ("dailyInputTokens", "weeklyInputTokens"):
        usage["limits"][name] = None
        usage[name] = None
    usage.update(
        usageUnits="tokens",
        tokenLimits={"total": 10000},
        tokens={
            "used": 12000,
            "remaining": -2000,
            "percentUsed": 1.2,
            "resetAt": 1893542400000,
        },
    )
    snapshot = nanogpt.parse_snapshot(usage)
    assert snapshot.entitlement_status == "verified"
    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].window_key == "tokens"
    assert snapshot.windows[0].used_ratio == 1.0  # DB ratio is bounded; native overuse survives in counters.
    assert snapshot.windows[0].limit_value == 10000
    assert snapshot.windows[0].used_value == 12000


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["catalog", "coverage"])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": [None]},
        {"data": [{"id": ""}]},
        ProviderError("synthetic-secret provider text", status=503),
    ],
)
async def test_nano_catalog_failure_is_safe_and_never_grants_coverage(monkeypatch, which, payload):
    install_nano(monkeypatch, **{which: payload})
    snapshot = await nanogpt.inspect("synthetic-nano")
    assert snapshot.entitlement_status == "unverified"
    assert all(not model.subscription_eligible for model in snapshot.models)
    assert "unavailable" in snapshot.detail
    assert "synthetic-secret" not in snapshot.detail
    if which == "coverage":
        assert len(snapshot.models) == 2
    else:
        assert snapshot.models == []


@pytest.mark.asyncio
async def test_nano_empty_coverage_and_aliases_do_not_invent_covered_models(monkeypatch):
    install_nano(monkeypatch, coverage={"data": [{"id": "native/included:thinking"}]})
    snapshot = await nanogpt.inspect("synthetic-nano")
    assert snapshot.entitlement_status == "unverified"
    assert not any(model.subscription_eligible for model in snapshot.models)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [nanogpt.USAGE_URL, nanogpt.CATALOG_URL, nanogpt.COVERAGE_URL])
async def test_nano_revoked_credentials_are_not_hidden_by_catalog_fallback(monkeypatch, endpoint):
    payloads = {nanogpt.USAGE_URL: NANO_USAGE, nanogpt.CATALOG_URL: NANO_CATALOG, nanogpt.COVERAGE_URL: NANO_COVERAGE}

    async def request(method, url, **kwargs):
        if url == endpoint:
            raise ProviderError("Provider metadata request rejected (HTTP 401)", permanent=True, status=401)
        return deepcopy(payloads[url])

    monkeypatch.setattr(nanogpt, "request_json", request)
    with pytest.raises(ProviderError) as caught:
        await nanogpt.inspect("synthetic-nano")
    assert caught.value.permanent is True
    assert caught.value.status == 401
    assert "synthetic-nano" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_deepseek_native_metered_balance_is_not_subscription_quota(monkeypatch, available):
    calls = []

    async def request(method, url, *, headers):
        assert method == "GET"
        assert headers == {"Authorization": "Bearer synthetic-deep", "Accept": "application/json"}
        assert url in {"https://api.deepseek.com/user/balance", "https://api.deepseek.com/models"}
        calls.append(url)
        if url == deepseek.BALANCE_URL:
            return {**DEEP_BALANCE, "is_available": available}
        return deepcopy(DEEP_CATALOG)

    monkeypatch.setattr(deepseek, "request_json", request)
    snapshot = await deepseek.inspect("synthetic-deep")
    assert calls == ["https://api.deepseek.com/user/balance", "https://api.deepseek.com/models"]
    assert snapshot.billing_mode == "api_metered"
    assert snapshot.entitlement_status == "unverified"
    assert snapshot.extra_usage_enabled is None
    assert snapshot.quota_group is None and snapshot.windows == []
    assert [model.model for model in snapshot.models] == ["native-deepseek", "deepseek-reasoner"]
    assert all(not model.subscription_eligible for model in snapshot.models)
    native, minimal = snapshot.models
    assert (native.context_window, native.max_output_tokens) == (234567, 12345)
    assert native.supports_vision is True and native.supports_tools is None
    assert json.loads(native.raw_metadata_json)["effort"]["default_level"] == "high"
    assert minimal.context_window is None and minimal.max_output_tokens is None
    assert minimal.supports_vision is None and minimal.supports_tools is None
    assert "subscription_verified" not in minimal.raw_metadata_json
    assert "3.25" not in snapshot.detail


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"is_available": "true", "balance_infos": []},
        {"is_available": True},
        {"is_available": False, "balance_infos": None},
    ],
)
def test_deepseek_invalid_balance_is_not_successful_metadata(payload):
    with pytest.raises(ProviderError, match="invalid balance metadata"):
        deepseek.parse_snapshot(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500, 503])
async def test_deepseek_catalog_errors_are_observed_without_response_echo(monkeypatch, status):
    async def request(method, url, **kwargs):
        if url == deepseek.BALANCE_URL:
            return deepcopy(DEEP_BALANCE)
        raise ProviderError("synthetic-secret provider body", status=status)

    monkeypatch.setattr(deepseek, "request_json", request)
    snapshot = await deepseek.inspect("synthetic-deep")
    assert snapshot.models == [] and snapshot.windows == []
    assert snapshot.billing_mode == "api_metered"
    assert f"HTTP {status}" in snapshot.detail
    assert "synthetic-secret" not in snapshot.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,endpoint",
    [
        (nanogpt, nanogpt.USAGE_URL),
        (deepseek, deepseek.BALANCE_URL),
    ],
)
async def test_primary_metadata_failures_propagate_safely_without_inference(monkeypatch, provider, endpoint):
    calls = []

    async def request(method, url, **kwargs):
        calls.append(url)
        raise ProviderError("Provider metadata temporarily unavailable", status=503)

    monkeypatch.setattr(provider, "request_json", request)
    with pytest.raises(ProviderError, match="temporarily unavailable") as caught:
        await provider.inspect("synthetic-secret")
    assert calls == [endpoint]
    assert caught.value.permanent is False
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,catalog,usage,coverage",
    [
        (nanogpt, nanogpt.CATALOG_URL, NANO_USAGE, NANO_COVERAGE),
        (deepseek, deepseek.CATALOG_URL, DEEP_BALANCE, None),
    ],
)
async def test_model_limits_require_positive_native_integers(monkeypatch, provider, catalog, usage, coverage):
    async def request(method, url, **kwargs):
        if url == catalog:
            return {
                "data": [
                    {"id": "native/included", "context_length": True, "context_window": "999", "max_output_tokens": -1}
                ]
            }
        if provider is nanogpt and url == nanogpt.COVERAGE_URL:
            return deepcopy(coverage)
        return deepcopy(usage)

    monkeypatch.setattr(provider, "request_json", request)
    snapshot = await provider.inspect("synthetic")
    assert len(snapshot.models) == 1
    assert snapshot.models[0].context_window is None
    assert snapshot.models[0].max_output_tokens is None


def test_nano_oversized_native_counter_stays_unknown():
    usage = deepcopy(NANO_USAGE)
    usage["dailyInputTokens"]["used"] = 10**400
    snapshot = nanogpt.parse_snapshot(usage)
    assert snapshot.entitlement_status == "unverified"
    assert snapshot.windows[0].used_value is None
    assert snapshot.windows[0].used_ratio is None


@pytest.mark.asyncio
async def test_deepseek_revoked_catalog_credential_propagates(monkeypatch):
    async def request(method, url, **kwargs):
        if url == deepseek.BALANCE_URL:
            return deepcopy(DEEP_BALANCE)
        raise ProviderError("Provider metadata request rejected (HTTP 401)", permanent=True, status=401)

    monkeypatch.setattr(deepseek, "request_json", request)
    with pytest.raises(ProviderError) as caught:
        await deepseek.inspect("synthetic-secret")
    assert caught.value.permanent is True
    assert caught.value.status == 401
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": [None]}, {"data": [{"id": ""}]}])
async def test_deepseek_malformed_catalog_never_seeds_defaults(monkeypatch, payload):
    async def request(method, url, **kwargs):
        return deepcopy(DEEP_BALANCE if url == deepseek.BALANCE_URL else payload)

    monkeypatch.setattr(deepseek, "request_json", request)
    snapshot = await deepseek.inspect("synthetic-secret")
    assert snapshot.billing_mode == "api_metered"
    assert snapshot.models == []
    assert snapshot.windows == []
    assert "catalog unavailable" in snapshot.detail


def test_nano_native_unavailable_routing_retains_exhaustion_without_eligibility():
    usage = deepcopy(NANO_USAGE)
    usage["dailyInputTokens"].update(used=1200, remaining=-200, percentUsed=1.2)
    usage["routing"].update(recommendedMode="unavailable", subscriptionQuotaAvailable=False)
    snapshot = nanogpt.parse_snapshot(usage)
    assert snapshot.entitlement_status == "blocked"
    assert snapshot.windows[0].used_ratio == 1.0
    assert snapshot.windows[0].used_value == 1200
    assert snapshot.windows[0].limit_value == 1000
