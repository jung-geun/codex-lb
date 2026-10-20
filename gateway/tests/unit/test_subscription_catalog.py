from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import ModelSource, ModelSourceModel, ModelSourceQuotaWindow
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.catalog import source_catalog_entries, source_model_cost_usd


def _key() -> ApiKeyData:
    return ApiKeyData(
        id="catalog-key",
        name="Catalog key",
        key_prefix="fixture",
        allowed_models=None,
        enforced_model=None,
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=datetime.now(UTC),
        last_used_at=None,
        billing_policy="subscription_only",
    )


def _source(source_id: str = "claude", model: str = "claude-fixture", *, native: bool = True) -> ModelSource:
    now = datetime.now(UTC)
    return ModelSource(
        id=source_id,
        name=source_id,
        priority=0,
        is_enabled=True,
        kind="anthropic_compatible" if native else "openai_compatible",
        provider="claude" if native else "zai",
        auth_type="api_key",
        api_key_encrypted=b"fixture",
        base_url="https://provider.example.invalid",
        billing_mode="subscription" if native else "coding_plan",
        entitlement_status="verified",
        entitlement_verified_at=now,
        extra_usage_enabled=False,
        quota_checked_at=now,
        quota_error=None,
        quota_group=None,
        cooldown_until=None,
        supports_messages=True,
        supports_chat_completions=not native,
        supports_responses=True,
        supports_audio_transcriptions=False,
        supports_embeddings=False,
        oauth_credential=None,
        quota_windows=[
            ModelSourceQuotaWindow(
                window_key="weekly",
                model=None,
                used_ratio=0.1,
                resets_at=now + timedelta(days=1),
                observed_at=now,
            )
        ],
        models=[
            ModelSourceModel(
                model=model,
                display_name=model,
                context_window=200_000,
                max_output_tokens=32_000,
                is_enabled=True,
                subscription_eligible=True,
                subscription_restricted=False,
                supports_streaming=True,
                supports_tools=True,
                supports_vision=True,
                raw_metadata_json=json.dumps(
                    {"supports_reasoning": True, "supported_reasoning_levels": ["low", "high"]}
                ),
            )
        ],
    )


def test_native_source_never_claims_codex_responses_even_if_flag_is_set() -> None:
    source = _source()
    [entry] = source_catalog_entries([source], _key())
    assert entry.protocols == ("messages",)
    assert entry.model.source_kind == "anthropic_compatible"
    assert entry.model.raw["api_types"] == ["messages"]
    assert source_catalog_entries([source], _key(), protocol="responses") == []


def test_eligible_glm_advertises_real_chat_and_messages_protocols() -> None:
    source = _source("glm", "glm-fixture", native=False)
    source.supports_responses = False
    [entry] = source_catalog_entries([source], _key())
    assert entry.protocols == ("chat_completions", "messages")
    assert entry.context_window == 200_000
    assert entry.max_output_tokens == 32_000
    assert source_catalog_entries([source], _key(), protocol="responses") == []


def test_catalog_enforces_exact_model_forced_model_and_empty_source_scope() -> None:
    sources = [_source(), _source("glm", "glm-fixture", native=False)]
    assert source_catalog_entries(sources, replace(_key(), source_assignment_scope_enabled=True)) == []
    assert source_catalog_entries(sources, replace(_key(), allowed_models=["claude"])) == []
    key = replace(
        _key(), enforced_model="glm-fixture", source_assignment_scope_enabled=True, assigned_source_ids=["glm"]
    )
    assert [entry.model.slug for entry in source_catalog_entries(sources, key)] == ["glm-fixture"]


@pytest.mark.parametrize("state", ["exhausted", "stale", "blocked", "unverified", "metered", "extra-usage", "cooldown"])
def test_catalog_uses_dispatch_entitlement_and_quota_policy(state: str) -> None:
    source = _source()
    if state == "exhausted":
        source.quota_windows[0].used_ratio = 1.0
    elif state == "stale":
        source.quota_checked_at = datetime.now(UTC) - timedelta(minutes=6)
    elif state in {"blocked", "unverified"}:
        source.entitlement_status = state
    elif state == "metered":
        source.billing_mode = "api_metered"
    elif state == "extra-usage":
        source.extra_usage_enabled = True
    else:
        source.cooldown_until = datetime.now(UTC) + timedelta(minutes=1)
    assert source_catalog_entries([source], _key()) == []


def test_catalog_checks_full_shared_quota_snapshot_before_scope_filtering() -> None:
    source, peer = _source(), _source("other", "other-model")
    source.quota_group = peer.quota_group = "shared-principal"
    peer.quota_windows[0].used_ratio = 1.0
    peer.is_enabled = False
    key = replace(_key(), source_assignment_scope_enabled=True, assigned_source_ids=[source.id])
    assert source_catalog_entries([source, peer], key) == []


def test_catalog_chooses_priority_then_skips_nonstreaming_for_stream_consumers() -> None:
    first, second = _source("first"), _source("second")
    second.priority = 10
    second.models[0].supports_streaming = False
    assert source_catalog_entries([first, second], _key())[0].model.source_id == "second"
    assert source_catalog_entries([first, second], _key(), require_streaming=True)[0].model.source_id == "first"


def test_responses_only_owner_does_not_mask_lower_priority_chat_owner() -> None:
    responses = _source("responses-owner", "shared-model", native=False)
    responses.priority = 10
    responses.supports_chat_completions = False
    responses.supports_messages = False
    chat = _source("chat-owner", "shared-model", native=False)
    chat.supports_responses = False
    entries = source_catalog_entries([responses, chat], _key(), require_streaming=True)
    assert [(entry.model.source_id, entry.protocols) for entry in entries] == [
        ("responses-owner", ("responses",)),
        ("chat-owner", ("chat_completions", "messages")),
    ]
    [entry] = source_catalog_entries([responses, chat], _key(), protocol="chat_completions")
    assert entry.model.source_id == "chat-owner"


def test_catalog_preserves_missing_true_limits_separately_from_legacy_defaults() -> None:
    source = _source()
    source.models[0].context_window = None
    source.models[0].max_output_tokens = None
    [entry] = source_catalog_entries([source], _key())
    assert entry.context_window is None
    assert entry.max_output_tokens is None


def test_source_cost_prices_uncached_read_and_both_cache_write_ttls() -> None:
    source = _source()
    entry = source.models[0]
    entry.input_per_1m, entry.cached_input_per_1m, entry.output_per_1m = 2.0, 0.2, 10.0
    entry.cache_creation_per_1m, entry.cache_creation_1h_per_1m = 2.5, 4.0
    cost = source_model_cost_usd(
        source,
        entry.model,
        input_tokens=1000,
        output_tokens=100,
        cached_input_tokens=200,
        cache_creation_input_tokens=500,
        cache_creation_5m_input_tokens=400,
        cache_creation_1h_input_tokens=100,
    )
    assert cost == pytest.approx((300 * 2 + 200 * 0.2 + 400 * 2.5 + 100 * 4 + 100 * 10) / 1_000_000)


@pytest.mark.parametrize(
    "missing",
    ["input_per_1m", "cached_input_per_1m", "output_per_1m", "cache_creation_per_1m", "cache_creation_1h_per_1m"],
)
def test_source_cost_never_fabricates_a_missing_consumed_rate(missing: str) -> None:
    source = _source()
    entry = source.models[0]
    entry.input_per_1m = entry.cached_input_per_1m = entry.output_per_1m = 1.0
    entry.cache_creation_per_1m = entry.cache_creation_1h_per_1m = 1.0
    setattr(entry, missing, None)
    assert (
        source_model_cost_usd(
            source,
            entry.model,
            input_tokens=1000,
            output_tokens=1,
            cached_input_tokens=100,
            cache_creation_input_tokens=200,
            cache_creation_5m_input_tokens=100,
            cache_creation_1h_input_tokens=100,
        )
        is None
    )


def test_source_cost_unknown_cache_ttl_stays_unknown() -> None:
    source = _source()
    source.models[0].input_per_1m = 1.0
    source.models[0].cache_creation_per_1m = 1.0
    assert (
        source_model_cost_usd(
            source, source.models[0].model, input_tokens=100, output_tokens=0, cache_creation_input_tokens=100
        )
        is None
    )
