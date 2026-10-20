from __future__ import annotations

import json

import pytest

from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import ProviderError

PROFILE = {
    "account": {"uuid": "synthetic-account", "has_claude_pro": True},
    "organization": {"uuid": "synthetic-org", "subscription_status": "active"},
}
USAGE = {
    "five_hour": {"utilization": 0, "resets_at": "2030-01-01T05:00:00Z"},
    "seven_day": {"utilization": 100, "resets_at": "2030-01-08T00:00:00Z"},
    "extra_usage": {"is_enabled": False},
}


def test_exhaustion_is_observation_not_fabricated_capacity_or_lost_entitlement():
    snapshot = claude.parse_snapshot(PROFILE, USAGE)
    assert snapshot.entitlement_status == "verified"
    assert snapshot.billing_mode == "subscription"
    assert snapshot.extra_usage_enabled is False
    assert snapshot.windows[0].used_ratio == 0
    assert snapshot.windows[1].used_ratio == 1
    assert snapshot.windows[1].limit_value is None
    assert snapshot.quota_group == "claude:synthetic-org:synthetic-account"


@pytest.mark.parametrize("extra", [None, {}, {"is_enabled": True}])
def test_unknown_or_paid_extra_usage_never_verifies_subscription(extra):
    snapshot = claude.parse_snapshot(PROFILE, {**USAGE, "extra_usage": extra})
    assert snapshot.entitlement_status != "verified"
    assert snapshot.models == []


def test_imported_claims_cannot_substitute_active_native_profile():
    profile = {
        "account": {"uuid": "synthetic-account"},
        "organization": {"uuid": "synthetic-org"},
        "subscriptionType": "max",
    }
    assert claude.parse_snapshot(profile, USAGE).entitlement_status == "unverified"


def test_scoped_usage_percent_one_is_one_percent():
    usage = {
        **USAGE,
        "limits": [
            {
                "group": "weekly",
                "scope": {"model": {"display_name": "Sonnet"}},
                "percent": 1,
                "resets_at": "2030-01-08T00:00:00Z",
            }
        ],
    }
    snapshot = claude.parse_snapshot(PROFILE, usage)
    scoped = next(window for window in snapshot.windows if window.model == "sonnet")
    assert scoped.used_ratio == 0.01


@pytest.mark.asyncio
async def test_catalog_is_account_native_even_when_weekly_exhausted(monkeypatch):
    calls = []

    async def request(method, url, *, headers, **kwargs):
        calls.append(url)
        assert method == "GET"
        assert headers["Authorization"] == "Bearer synthetic"
        if "/profile" in url:
            return PROFILE
        if "/usage" in url:
            return USAGE
        return {
            "data": [
                {
                    "id": "native-model",
                    "line": "sonnet",
                    "max_input_tokens": 200000,
                    "max_tokens": 32000,
                    "capabilities": {"thinking": {"supported": True, "types": {"adaptive": {"supported": True}}}},
                }
            ],
            "has_more": False,
        }

    monkeypatch.setattr(claude, "request_json", request)
    snapshot = await claude.inspect("synthetic")
    assert len(calls) == 3
    assert snapshot.models[0].model == "native-model"
    assert snapshot.models[0].context_window == 200000
    assert snapshot.models[0].subscription_eligible is True
    assert json.loads(snapshot.models[0].raw_metadata_json)["capabilities"]["thinking"]["types"]["adaptive"][
        "supported"
    ]


@pytest.mark.asyncio
async def test_rejected_model_endpoint_does_not_seed_fake_models(monkeypatch):
    async def request(method, url, **kwargs):
        if "/profile" in url:
            return PROFILE
        if "/usage" in url:
            return USAGE
        raise ProviderError("Provider metadata request rejected (HTTP 403)", status=403)

    monkeypatch.setattr(claude, "request_json", request)
    snapshot = await claude.inspect("synthetic")
    assert snapshot.models == []
    assert "403" in snapshot.detail


def test_team_provisioned_extra_usage_is_not_effective_billing_permission():
    profile = {
        "account": {"uuid": "synthetic-account"},
        "organization": {
            "uuid": "synthetic-org",
            "organization_type": "claude_team",
            "billing_type": "stripe_subscription",
            "seat_tier": "team_labs_premium",
            "subscription_status": "active",
            "rate_limit_tier": "default_claude_max_5x",
            "has_extra_usage_enabled": True,
        },
    }
    usage = {
        **USAGE,
        "five_hour": {"utilization": 0, "resets_at": None},
        "extra_usage": {
            "is_enabled": False,
            "disabled_reason": "org_level_disabled_until",
            "user_disabled": False,
            "spend_limit_reached": False,
        },
    }
    snapshot = claude.parse_snapshot(profile, usage)
    assert snapshot.subscription_type == "team"
    assert snapshot.entitlement_status == "verified"
    assert snapshot.extra_usage_enabled is False
    assert snapshot.windows[0].resets_at is None
    assert snapshot.windows[1].used_ratio == 1
    assert "provisioned" in snapshot.detail


def test_unknown_effective_extra_usage_does_not_use_profile_provisioning_as_false():
    profile = {**PROFILE, "organization": {**PROFILE["organization"], "has_extra_usage_enabled": False}}
    snapshot = claude.parse_snapshot(profile, {**USAGE, "extra_usage": {}})
    assert snapshot.extra_usage_enabled is None
    assert snapshot.entitlement_status == "unverified"


@pytest.mark.asyncio
async def test_refresh_preserves_previous_token_when_provider_does_not_rotate(monkeypatch):
    async def request(method, url, *, headers, json):
        assert url == "https://platform.claude.com/v1/oauth/token"
        assert json == {"grant_type": "refresh_token", "refresh_token": "synthetic-old", "client_id": claude.CLIENT_ID}
        return {"access_token": "synthetic-new", "refresh_token": "", "expires_in": 3600}

    monkeypatch.setattr(claude, "request_json", request)
    pair = await claude.refresh("synthetic-old")
    assert pair.refresh_token.get_secret_value() == "synthetic-old"
    assert pair.expires_in == 3600
    assert "synthetic-new" not in repr(pair)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,permanent", [(400, True), (401, True), (403, True), (429, False), (500, False)])
async def test_refresh_distinguishes_permanent_from_transient_without_echo(monkeypatch, status, permanent):
    async def request(*args, **kwargs):
        raise ProviderError(f"Provider metadata request rejected (HTTP {status})", status=status)

    monkeypatch.setattr(claude, "request_json", request)
    with pytest.raises(ProviderError) as caught:
        await claude.refresh("synthetic-secret")
    assert caught.value.permanent is permanent
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_expiry_is_not_replaced_with_invented_lifetime(monkeypatch):
    async def request(*args, **kwargs):
        return {"access_token": "synthetic-secret"}

    monkeypatch.setattr(claude, "request_json", request)
    with pytest.raises(ProviderError, match="invalid token response") as caught:
        await claude.refresh("synthetic-refresh")
    assert "synthetic-secret" not in str(caught.value)
