from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import ModelSource, ModelSourceModel, ModelSourceOAuthCredential, ModelSourceQuotaWindow
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.selection import (
    release_source_slot, source_capacity_group, source_has_capacity, source_is_eligible, try_acquire_source_slot,
)


def key(**changes) -> ApiKeyData:
    return replace(ApiKeyData(
        id="key", name="key", key_prefix="test", allowed_models=None, enforced_model=None,
        enforced_reasoning_effort=None, enforced_service_tier=None, expires_at=None,
        is_active=True, created_at=datetime.now(UTC), last_used_at=None, billing_policy="subscription_only",
    ), **changes)


def source(source_id="one") -> ModelSource:
    now = datetime.now(UTC)
    return ModelSource(
        id=source_id, name=source_id, kind="anthropic_compatible", base_url="https://api.anthropic.com",
        provider="claude", auth_type="claude_oauth", billing_mode="subscription", is_enabled=True,
        entitlement_status="verified", entitlement_verified_at=now, extra_usage_enabled=False,
        supports_messages=True, supports_chat_completions=False, supports_responses=False,
        supports_audio_transcriptions=False, supports_embeddings=False, cooldown_until=None,
        quota_checked_at=now, quota_error=None, quota_group="group", max_concurrency=1,
        oauth_credential=ModelSourceOAuthCredential(source_id=source_id, access_token_encrypted=b"secret",
            expires_at=now + timedelta(hours=1), account_uuid="principal", organization_uuid="org", generation=1),
        models=[ModelSourceModel(model="claude-model", is_enabled=True, supports_streaming=True,
            subscription_eligible=True, subscription_restricted=False)],
        quota_windows=[ModelSourceQuotaWindow(window_key="weekly", model=None, used_ratio=.5,
            observed_at=now, resets_at=now + timedelta(days=1))],
    )


def test_verified_plan_is_native_only_and_key_scoped():
    row = source()
    assert source_is_eligible(row, key(), "claude-model", protocol="messages")
    assert not source_is_eligible(row, key(), "claude-model", protocol="responses")
    assert source_is_eligible(row, key(allowed_models=[]), "claude-model", protocol="messages")
    assert not source_is_eligible(row, key(allowed_models=["claude"]), "claude-model", protocol="messages")
    assert not source_is_eligible(row, key(source_assignment_scope_enabled=True), "claude-model", protocol="messages")
    assert source_is_eligible(row, key(source_assignment_scope_enabled=True, assigned_source_ids=[row.id]), "claude-model", protocol="messages")


@pytest.mark.parametrize("allowed_models", [None, [], ["claude-model", "other-model"]])
def test_enforced_model_intersects_source_and_allowed_models(allowed_models):
    row = source()
    assert source_is_eligible(
        row, key(allowed_models=allowed_models, enforced_model="claude-model"), "claude-model", protocol="messages"
    )
    assert not source_is_eligible(
        row, key(allowed_models=allowed_models, enforced_model="other-model"), "claude-model", protocol="messages"
    )
    assert not source_is_eligible(
        row, key(allowed_models=["other-model"], enforced_model="claude-model"), "claude-model", protocol="messages"
    )


@pytest.mark.parametrize("field,value", [
    ("entitlement_status", "unverified"), ("entitlement_status", "reauth_required"),
    ("entitlement_status", "blocked"), ("entitlement_verified_at", None),
    ("extra_usage_enabled", True), ("extra_usage_enabled", None), ("quota_checked_at", None),
    ("quota_error", "provider unavailable"), ("is_enabled", False), ("oauth_credential", None),
    ("billing_mode", "api_metered"), ("billing_mode", "unknown"),
])
def test_uncertain_or_metered_plan_is_not_capacity(field, value):
    row = source()
    setattr(row, field, value)
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")


@pytest.mark.parametrize("ratio", [None, 1.0, float("nan"), float("inf"), -.01, 1.01])
def test_unknown_invalid_and_exhausted_windows_fail_closed(ratio):
    row = source()
    row.quota_windows[0].used_ratio = ratio
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")


def test_reset_does_not_invent_replenishment_and_refresh_failure_is_honest():
    row = source()
    window = row.quota_windows[0]
    window.used_ratio = 1
    window.resets_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")
    window.used_ratio = .1
    window.resets_at = datetime.now(UTC) + timedelta(hours=1)
    row.quota_error = "usage endpoint rejected"
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.quota_error = None
    assert source_is_eligible(row, key(), "claude-model", protocol="messages")


def test_stale_provider_observation_is_not_available_quota():
    row = source()
    row.quota_checked_at = datetime.now(UTC) - timedelta(minutes=6)
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.quota_checked_at = datetime.now(UTC)
    row.quota_windows[0].observed_at = datetime.now(UTC) - timedelta(minutes=6)
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")


def test_model_specific_window_and_unknown_limits_are_not_global_capacity():
    row = source()
    row.quota_windows[0].model = "other-model"
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.quota_windows[0].model = None
    row.quota_windows[0].used_ratio = None
    row.quota_windows[0].limit_value = 100
    row.quota_windows[0].used_value = 50
    assert source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.quota_windows[0].used_value = None
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")


def test_same_principal_cannot_escape_exhaustion_with_another_key():
    first, second = source(), source("two")
    second.quota_group = "different-operator-label"
    first.quota_windows[0].used_ratio = 1
    assert source_capacity_group(first) == source_capacity_group(second)
    assert not source_is_eligible(second, key(), "claude-model", protocol="messages", group_sources=[first, second])
    first.quota_windows[0].used_ratio = .2
    assert source_is_eligible(second, key(), "claude-model", protocol="messages", group_sources=[first, second])


def test_unrelated_model_exhaustion_does_not_block_this_model():
    row = source()
    row.quota_windows.append(ModelSourceQuotaWindow(window_key="other", model="other-model", used_ratio=1,
        observed_at=datetime.now(UTC), resets_at=datetime.now(UTC) + timedelta(days=1)))
    assert source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.models[0].subscription_restricted = True
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")


def test_same_principal_shares_concurrency_and_released_slot_is_reusable():
    first, second = source("concurrency-one"), source("concurrency-two")
    assert try_acquire_source_slot(first)
    try:
        assert not source_has_capacity(second)
        assert not try_acquire_source_slot(second)
    finally:
        release_source_slot(first)
    assert try_acquire_source_slot(second)
    release_source_slot(second)


def test_cooldown_and_model_coverage_are_enforced():
    row = source()
    row.cooldown_until = datetime.now(UTC) + timedelta(seconds=30)
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
    assert source_is_eligible(row, key(), "claude-model", protocol="messages")
    row.models[0].subscription_eligible = False
    assert not source_is_eligible(row, key(), "claude-model", protocol="messages")


def test_legacy_keyless_custom_source_stays_compatible_but_not_subscription():
    row = source()
    row.provider = "custom"
    row.auth_type = "none"
    row.billing_mode = "unknown"
    row.entitlement_status = "unverified"
    row.oauth_credential = None
    row.quota_windows = []
    row.models[0].model = "local-model"
    assert source_is_eligible(row, key(billing_policy="any"), "local-model", protocol="messages")
    assert not source_is_eligible(row, key(), "local-model", protocol="messages")


def test_shared_group_across_distinct_principals_is_conservative():
    first, second = source("shared-one"), source("shared-two")
    second.oauth_credential.account_uuid = "different-principal"
    first.quota_windows[0].used_ratio = 1
    assert not source_is_eligible(second, key(), "claude-model", protocol="messages", group_sources=[first, second])


def test_durable_dispatching_or_unknown_work_occupies_shared_capacity():
    first, second = source("durable-one"), source("durable-two")
    assert not source_has_capacity(second, group_sources=[first, second], active_operation_counts={first.id: 1})
    assert source_has_capacity(second, group_sources=[first, second], active_operation_counts={})


def test_any_key_can_fallback_from_exhausted_plan_to_exact_metered_copy():
    plan, paid = source("plan"), source("paid")
    plan.quota_windows[0].used_ratio = 1
    paid.provider = "anthropic"
    paid.auth_type = "api_key"
    paid.api_key_encrypted = b"encrypted"
    paid.oauth_credential = None
    paid.billing_mode = "api_metered"
    paid.models[0].model = "anthropic/claude-model"
    paid.models[0].upstream_model = "claude-model"
    paid.api_rate_limits = []
    paid.quota_windows = []
    assert source_is_eligible(paid, key(billing_policy="any"), "claude-model", protocol="messages", group_sources=[plan, paid])
    assert not source_is_eligible(paid, key(), "claude-model", protocol="messages", group_sources=[plan, paid])


def test_old_reauth_failure_does_not_poison_fresh_duplicate_principal():
    old, healthy = source("old-credential"), source("healthy-credential")
    old.is_enabled = False
    old.entitlement_status = "reauth_required"
    old.quota_error = "old credential revoked"
    old.quota_checked_at = datetime.now(UTC) - timedelta(hours=1)
    old.quota_windows[0].observed_at = old.quota_checked_at
    assert source_is_eligible(healthy, key(), "claude-model", protocol="messages", group_sources=[old, healthy])


def test_older_exhaustion_requires_actual_post_reset_provider_observation():
    old, healthy = source("exhausted-old"), source("fresh-new")
    now = datetime.now(UTC)
    old.entitlement_status = "reauth_required"
    old.quota_windows[0].used_ratio = 1
    old.quota_windows[0].observed_at = now - timedelta(minutes=10)
    old.quota_windows[0].resets_at = now + timedelta(minutes=1)
    assert not source_is_eligible(healthy, key(), "claude-model", protocol="messages", group_sources=[old, healthy])
    old.quota_windows[0].resets_at = now - timedelta(minutes=1)
    healthy.quota_windows[0].observed_at = now
    assert source_is_eligible(healthy, key(), "claude-model", protocol="messages", group_sources=[old, healthy])


def test_explicit_deepseek_metered_support_is_any_only():
    row = source("deepseek")
    row.provider = "deepseek"
    row.auth_type = "api_key"
    row.api_key_encrypted = b"encrypted"
    row.billing_mode = "api_metered"
    row.entitlement_status = "unverified"
    row.models[0].model = "deepseek-chat"
    assert source_is_eligible(row, key(billing_policy="any"), "deepseek-chat", protocol="messages")
    assert not source_is_eligible(row, key(), "deepseek-chat", protocol="messages")


def test_unidentified_plan_keys_default_to_one_conservative_provider_capacity():
    first, second = source("unknown-first"), source("unknown-second")
    for row in (first, second):
        row.provider = "zai"
        row.auth_type = "api_key"
        row.api_key_encrypted = b"encrypted"
        row.oauth_credential = None
        row.quota_group = None
        row.billing_mode = "coding_plan"
    assert source_capacity_group(first) == source_capacity_group(second)
    assert not source_has_capacity(second, group_sources=[first, second], active_operation_counts={first.id: 1})
    first.quota_windows[0].used_ratio = 1
    assert not source_is_eligible(second, key(), "claude-model", protocol="messages", group_sources=[first, second])
