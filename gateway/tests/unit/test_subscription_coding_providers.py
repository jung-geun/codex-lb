from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from app.modules.model_sources.providers import kimi, zai
from app.modules.model_sources.providers.base import ProviderError

# Synthetic values in the observed TeamClaude pinned 88fe9b3 wire contracts,
# not assertions about a real account's subscription or key classification.
GLM_USAGE = {
    "code": 200,
    "data": {
        "level": "pro",
        "limits": [
            {
                "type": "TOKENS_LIMIT",
                "unit": 3,
                "number": 5,
                "percentage": 12,
                "total": 1000,
                "nextResetTime": 1893474000000,
            },
            {
                "type": "TOKENS_LIMIT",
                "unit": 6,
                "number": 1,
                "percentage": 100,
                "total": 5000,
                "nextResetTime": 1894060800000,
            },
            {"type": "TIME_LIMIT", "percentage": 4, "currentValue": 3, "usage": 12},
        ],
    },
}
KIMI_USAGE = {
    "usage": {"limit": "100", "used": "12", "remaining": "88", "resetTime": "2030-01-08T00:00:00Z"},
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "used": "52", "remaining": "48", "resetTime": "2030-01-01T05:00:00Z"},
        }
    ],
    "usages": {
        "limit_5h": {"used_ratio": 0, "reset_time": "2030-01-01T04:59:59Z"},
        "limit_7d": {"used_ratio": 0, "reset_time": "2030-01-07T23:59:59Z"},
    },
}
KIMI_MODELS = {
    "data": [
        {
            "id": "provider-native-model",
            "display_name": "Native model",
            "context_length": 262144,
            "supports_reasoning": True,
            "supports_image_in": True,
            "supports_video_in": False,
        }
    ]
}


def assert_unverified(snapshot):
    assert snapshot.entitlement_status == "unverified"
    assert snapshot.billing_mode == "unknown"
    assert snapshot.extra_usage_enabled is None
    assert snapshot.subscription_type is None
    assert snapshot.detected_key_type == "unknown"
    assert snapshot.quota_group is None
    assert all(model.subscription_eligible is False for model in snapshot.models)


def test_glm_observes_windows_not_balance_or_key_classification():
    snapshot = zai.parse_snapshot(GLM_USAGE)
    assert_unverified(snapshot)
    assert [(window.window_key, window.used_ratio) for window in snapshot.windows] == [
        ("five_hour", 0.12),
        ("seven_day", 1.0),
    ]
    assert snapshot.windows[0].resets_at == datetime(2030, 1, 1, 5)
    assert all(window.limit_value is None and window.used_value is None for window in snapshot.windows)
    assert "classification of this exact API key" in snapshot.detail
    assert "active plan/seat" in snapshot.detail
    assert "Team on-demand overage" in snapshot.detail


@pytest.mark.parametrize("claimed_type", ["Standard", "Compatible", "Team Plan", "Individual Coding Plan"])
def test_glm_no_invented_key_type_or_overage_fields_can_unlock(claimed_type):
    body = copy.deepcopy(GLM_USAGE)
    # These are intentionally NOT known provider API contracts. A convenient
    # fabricated label must not become a subscription verification mechanism.
    body["data"].update(key_type=claimed_type, plan_active=True, overage_enabled=False)
    body.update(subscriptionType="coding", billing_mode="subscription")
    assert_unverified(zai.parse_snapshot(body))


@pytest.mark.parametrize("value", [None, "", True, "NaN", float("inf"), -1, [], {}])
def test_glm_invalid_percentage_never_becomes_zero_capacity(value):
    snapshot = zai.parse_snapshot(
        {
            "code": 200,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": value},
                ]
            },
        }
    )
    assert snapshot.windows == []
    assert "no usable token windows" in snapshot.detail
    assert_unverified(snapshot)


def test_glm_unknown_windows_and_mcp_are_not_invented_as_plan_windows():
    body = {
        "code": 200,
        "data": {
            "limits": [
                {"type": "TIME_LIMIT", "unit": 3, "number": 5, "percentage": 20},
                {"type": "TOKENS_LIMIT", "unit": 6, "number": True, "percentage": 20},
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 24, "percentage": 20},
                {"type": "TOKENS_LIMIT", "unit": [], "number": 5, "percentage": 20},
            ]
        },
    }
    assert zai.parse_snapshot(body).windows == []


def test_glm_exhausted_window_without_reset_is_still_observed():
    snapshot = zai.parse_snapshot(
        {
            "code": 200,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 130, "nextResetTime": 0},
                ]
            },
        }
    )
    assert snapshot.windows[0].used_ratio == 1
    assert snapshot.windows[0].resets_at is None
    assert_unverified(snapshot)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"code": 401, "data": GLM_USAGE["data"]},
        {"code": 200, "success": False, "data": GLM_USAGE["data"]},
        {"code": 200, "data": {"limits": None}},
    ],
)
def test_glm_application_error_cannot_reuse_quota_data(body):
    snapshot = zai.parse_snapshot(body)
    assert snapshot.windows == []
    assert_unverified(snapshot)


@pytest.mark.asyncio
async def test_glm_only_fixed_known_urls_with_raw_monitor_and_bearer_catalog(monkeypatch):
    calls = []

    async def request(method, url, *, headers):
        calls.append(url)
        assert method == "GET"
        if url == "https://api.z.ai/api/monitor/usage/quota/limit":
            assert headers["Authorization"] == "synthetic-secret"
            assert headers["Accept-Language"] == "en-US,en"
            return GLM_USAGE
        assert url == "https://api.z.ai/api/paas/v4/models"
        assert headers["Authorization"] == "Bearer synthetic-secret"
        return {"data": [{"id": "native-glm"}]}

    monkeypatch.setattr(zai, "request_json", request)
    snapshot = await zai.inspect("synthetic-secret")
    assert calls == [zai.MONITOR_URL, zai.CATALOG_URL]
    assert [model.model for model in snapshot.models] == ["native-glm"]
    assert snapshot.models[0].context_window is None
    metadata = json.loads(snapshot.models[0].raw_metadata_json)
    assert metadata["coding_plan_coverage_verified"] is False
    assert "synthetic-secret" not in snapshot.detail
    assert_unverified(snapshot)


def test_kimi_lagging_zero_ratio_uses_reliable_matching_counters():
    snapshot = kimi.parse_snapshot(KIMI_USAGE)
    assert [
        (window.window_key, window.used_ratio, window.used_value, window.limit_value) for window in snapshot.windows
    ] == [("five_hour", 0.52, 52, 100), ("seven_day", 0.12, 12, 100)]
    assert snapshot.windows[0].resets_at == datetime(2030, 1, 1, 5)
    assert_unverified(snapshot)


@pytest.mark.parametrize(
    "delta,weekly,monthly,expected",
    [
        ("2030-01-01T04:59:58Z", True, False, 0.52),
        ("2030-01-01T04:59:57Z", True, False, 0),
        ("2030-01-01T04:59:59Z", False, False, 0),
        ("2030-01-01T04:59:59Z", True, True, 0),
    ],
)
def test_kimi_stuck_ratio_resolution_preserves_pinned_guards(delta, weekly, monthly, expected):
    body = copy.deepcopy(KIMI_USAGE)
    body["usages"]["limit_5h"]["reset_time"] = delta
    if not weekly:
        body["usage"] = {"limit": "100"}
    if monthly:
        body["usages"]["limit_month_total"] = {"used_ratio": 0.01, "reset_time": "2030-02-01T00:00:00Z"}
    snapshot = kimi.parse_snapshot(body)
    assert snapshot.windows[0].used_ratio == expected
    assert_unverified(snapshot)


@pytest.mark.parametrize(
    "pools,expected",
    [
        ({}, []),
        ({"limit_month_total": {"used_ratio": 1.05}}, [("monthly", 1)]),
        ({"limit_5h": {"used_ratio": 0.01}}, [("five_hour", 0.01)]),
        ({"limit_7d": {"used_ratio": 0.3}}, [("seven_day", 0.3)]),
    ],
)
def test_kimi_named_pool_set_does_not_invent_absent_legacy_windows(pools, expected):
    body = {**KIMI_USAGE, "usages": pools}
    snapshot = kimi.parse_snapshot(body)
    assert [(window.window_key, window.used_ratio) for window in snapshot.windows] == expected
    assert_unverified(snapshot)


@pytest.mark.parametrize(
    "unit,duration", [("TIME_UNIT_MINUTE", "300"), ("TIME_UNIT_HOUR", 5), ("TIME_UNIT_DAY", 300 / 1440)]
)
def test_kimi_legacy_counters_use_exact_five_hour_duration(unit, duration):
    body = copy.deepcopy(KIMI_USAGE)
    del body["usages"]
    body["limits"][0]["window"] = {"duration": duration, "timeUnit": unit}
    snapshot = kimi.parse_snapshot(body)
    assert [(window.window_key, window.used_ratio) for window in snapshot.windows] == [
        ("five_hour", 0.52),
        ("seven_day", 0.12),
    ]


def test_kimi_counter_preserves_overage_usage_and_remaining_fallback():
    snapshot = kimi.parse_snapshot({"usage": {"limit": "100", "used": "125", "remaining": "0"}})
    assert snapshot.windows[0].used_value == 125
    assert snapshot.windows[0].used_ratio == 1
    snapshot = kimi.parse_snapshot({"usage": {"limit": "100", "remaining": "75"}})
    assert snapshot.windows[0].used_value == 25
    assert snapshot.windows[0].used_ratio == 0.25
    assert_unverified(snapshot)


@pytest.mark.parametrize(
    "detail",
    [
        {"limit": 100},
        {"limit": 0, "used": 0},
        {"limit": 100, "used": None, "remaining": 101},
        {"limit": True, "used": 0},
        {"limit": "NaN", "used": 0},
    ],
)
def test_kimi_unreliable_counters_do_not_invent_zero_usage(detail):
    snapshot = kimi.parse_snapshot({"usage": detail})
    assert snapshot.windows == []
    assert "no usable quota windows" in snapshot.detail


@pytest.mark.parametrize("ratio", [None, "", True, -1, "NaN"])
def test_kimi_invalid_ratio_falls_back_to_named_reliable_counter(ratio):
    snapshot = kimi.parse_snapshot({"usage": KIMI_USAGE["usage"], "usages": {"limit_7d": {"used_ratio": ratio}}})
    assert snapshot.windows[0].used_ratio == 0.12


def test_kimi_only_native_catalog_fields_are_persisted_without_plan_claims():
    body = copy.deepcopy(KIMI_MODELS)
    body["data"].extend(
        [None, {"id": ""}, {"id": "provider-native-model"}, {"id": "no-capabilities", "context_length": True}]
    )
    body["data"][0].update(
        subscription_eligible=True, extra_usage_enabled=False, api_key="synthetic-secret", max_output_tokens=32000
    )
    models = kimi.parse_catalog(body)
    assert [model.model for model in models] == ["provider-native-model", "no-capabilities"]
    assert models[0].context_window == 262144
    assert models[0].display_name == "Native model"
    assert models[0].supports_vision is True
    assert models[0].supports_tools is None
    assert models[0].max_output_tokens is None
    assert models[1].context_window is None
    assert models[1].supports_vision is None
    metadata = json.loads(models[0].raw_metadata_json)
    assert metadata["supports_reasoning"] is True
    assert metadata["supports_video_in"] is False
    assert "synthetic-secret" not in models[0].raw_metadata_json
    assert all(model.subscription_eligible is False for model in models)


@pytest.mark.asyncio
async def test_kimi_only_official_cli_metadata_urls_no_inference_or_guessed_billing_probes(monkeypatch):
    calls = []

    async def request(method, url, *, headers):
        calls.append(url)
        assert method == "GET"
        assert headers == {"Authorization": "Bearer synthetic-secret", "Accept": "application/json"}
        if url == "https://api.kimi.com/coding/v1/usages":
            return KIMI_USAGE
        assert url == "https://api.kimi.com/coding/v1/models"
        return KIMI_MODELS

    monkeypatch.setattr(kimi, "request_json", request)
    snapshot = await kimi.inspect("synthetic-secret")
    assert calls == [kimi.USAGE_URL, kimi.CATALOG_URL]
    assert len(snapshot.windows) == 2
    assert len(snapshot.models) == 1
    assert "active membership" in snapshot.detail
    assert "plan-specific model coverage" in snapshot.detail
    assert "Extra Usage/pay-as-you-go-disabled" in snapshot.detail
    assert "synthetic-secret" not in snapshot.detail
    assert_unverified(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,usage_url,usage,models",
    [
        (zai, zai.MONITOR_URL, GLM_USAGE, {"data": [{"id": "native-glm"}]}),
        (kimi, kimi.USAGE_URL, KIMI_USAGE, KIMI_MODELS),
    ],
)
@pytest.mark.parametrize(
    "failed_endpoint,status", [("usage", 401), ("usage", 404), ("usage", 500), ("catalog", 403), ("catalog", 429)]
)
async def test_partial_endpoint_failure_preserves_other_truthful_observations(
    monkeypatch,
    provider,
    usage_url,
    usage,
    models,
    failed_endpoint,
    status,
):
    calls = []

    async def request(method, url, **kwargs):
        calls.append(url)
        is_usage = url == usage_url
        if is_usage == (failed_endpoint == "usage"):
            raise ProviderError(
                f"Provider metadata request rejected (HTTP {status})", status=status, permanent=status == 401
            )
        return usage if is_usage else models

    monkeypatch.setattr(provider, "request_json", request)
    snapshot = await provider.inspect("synthetic")
    assert calls == [usage_url, provider.CATALOG_URL]
    assert str(status) in snapshot.detail
    assert bool(snapshot.models) is (failed_endpoint == "usage")
    assert bool(snapshot.windows) is (failed_endpoint == "catalog")
    assert_unverified(snapshot)


@pytest.mark.parametrize("provider", [zai, kimi])
@pytest.mark.parametrize("body", [{}, {"data": {}}, {"error": {"message": "private"}, "data": []}])
def test_invalid_catalog_never_seeds_fallback_models_or_echoes_provider_error(provider, body):
    with pytest.raises(ProviderError) as caught:
        provider.parse_catalog(body)
    assert "private" not in str(caught.value)
