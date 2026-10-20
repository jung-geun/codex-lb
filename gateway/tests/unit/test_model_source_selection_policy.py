from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db.models import ModelSource, ModelSourceModel, ModelSourceOAuthCredential, ModelSourceQuotaWindow
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources import selection, subscription_runtime
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.repository import ModelSourcesRepository

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
MODEL = "source-policy-model"
POLICIES = ("burn_first", "normal", "preserve")


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None) -> datetime:
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@dataclass
class _Catalog(ModelSourcesRepository):
    sources: list[ModelSource] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    async def list_sources(self) -> list[ModelSource]:
        return self.sources

    async def active_operation_counts(self) -> dict[str, int]:
        return self.counts


def _source(source_id: str, policy: str = "normal", *, priority: int = 0) -> ModelSource:
    return ModelSource(
        id=source_id,
        name=source_id,
        kind="anthropic_compatible",
        base_url="https://api.anthropic.com",
        provider="claude",
        auth_type="claude_oauth",
        billing_mode="subscription",
        routing_policy=policy,
        priority=priority,
        is_enabled=True,
        entitlement_status="verified",
        entitlement_verified_at=NOW,
        extra_usage_enabled=False,
        supports_messages=True,
        supports_chat_completions=False,
        supports_responses=False,
        supports_audio_transcriptions=False,
        supports_embeddings=False,
        quota_checked_at=NOW,
        quota_error=None,
        quota_group=None,
        cooldown_until=None,
        max_concurrency=1,
        oauth_credential=ModelSourceOAuthCredential(
            source_id=source_id,
            access_token_encrypted=b"encrypted",
            expires_at=NOW + timedelta(hours=1),
            account_uuid=source_id,
            organization_uuid="org",
            generation=1,
        ),
        models=[
            ModelSourceModel(
                model=MODEL,
                is_enabled=True,
                supports_streaming=True,
                subscription_eligible=True,
                subscription_restricted=False,
            )
        ],
        quota_windows=[
            ModelSourceQuotaWindow(
                window_key="seven_day",
                used_ratio=0.5,
                observed_at=NOW,
                resets_at=NOW + timedelta(days=1),
            )
        ],
    )


@pytest.fixture(autouse=True)
def _isolate_selection_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selection, "datetime", _FrozenDateTime)
    monkeypatch.setattr(selection, "_affinity", OrderedDict())
    monkeypatch.setattr(selection, "_last_selected", OrderedDict())
    monkeypatch.setattr(selection, "_selection_sequence", 0)
    monkeypatch.setattr(selection, "_source_inflight", {})
    monkeypatch.setattr(selection, "_group_inflight", {})
    monkeypatch.setattr(subscription_runtime, "utcnow", lambda: NOW.replace(tzinfo=None))
    monkeypatch.setattr(selection, "time", SimpleNamespace(monotonic=lambda: 1.0))
    monkeypatch.setattr(
        selection, "get_model_registry",
        lambda: SimpleNamespace(get_models_with_fallback=lambda: {"gpt-5.4": None}),
    )


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> _Catalog:
    catalog = _Catalog()

    @asynccontextmanager
    async def session() -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(selection, "get_background_session", session)
    monkeypatch.setattr(selection, "ModelSourcesRepository", lambda _session: catalog)
    monkeypatch.setattr(selection, "detach_session_objects", lambda _session: None)
    return catalog


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policies,expected",
    [
        (("preserve", "normal", "burn_first"), "burn_first"),
        (("normal", "preserve", "burn_first"), "burn_first"),
        (("preserve", "normal"), "normal"),
        (("normal", "preserve"), "normal"),
        (("preserve",), "preserve"),
    ],
)
async def test_policy_tiers_override_priority(catalog: _Catalog, policies: tuple[str, ...], expected: str) -> None:
    # Both numeric priority and usage favor the lower-policy candidates.
    for policy in policies:
        source = _source(policy, policy, priority=POLICIES.index(policy) * 100)
        source.quota_windows[0].used_ratio = 0.9 - POLICIES.index(policy) * 0.4
        catalog.sources.append(source)

    selected = await selection.select_model_source(MODEL, None, protocol="messages")

    assert selected is not None and selected.id == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", POLICIES)
async def test_priority_remains_primary_within_policy(catalog: _Catalog, policy: str) -> None:
    low = _source("low", policy, priority=1)
    high = _source("high", policy, priority=2)
    low.quota_windows[0].used_ratio = 0.1
    high.quota_windows[0].used_ratio = 0.9
    catalog.sources[:] = [low, high]

    assert await selection.select_model_source(MODEL, None, protocol="messages") is high


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", POLICIES)
async def test_priority_ties_keep_headroom_reset_and_rotation_order(catalog: _Catalog, policy: str) -> None:
    first = _source("a-first", policy)
    second = _source("z-second", policy)
    catalog.sources[:] = [first, second]
    first.quota_windows[0].used_ratio = 0.8
    second.quota_windows[0].used_ratio = 0.2
    assert await selection.select_model_source(MODEL, None, protocol="messages") is second

    second.quota_windows[0].used_ratio = 0.8
    first.quota_windows[0].resets_at = NOW + timedelta(days=2)
    assert await selection.select_model_source(MODEL, None, protocol="messages") is second

    first.quota_windows[0].resets_at = second.quota_windows[0].resets_at
    assert await selection.select_model_source(MODEL, None, protocol="messages") is first
    assert await selection.select_model_source(MODEL, None, protocol="messages") is second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous_policy,new_policy",
    [("normal", "burn_first"), ("preserve", "burn_first"), ("preserve", "normal")],
)
async def test_higher_policy_overrides_and_rebinds_affinity(
    catalog: _Catalog, previous_policy: str, new_policy: str
) -> None:
    previous = _source("previous", previous_policy, priority=100)
    preferred = _source("preferred", new_policy, priority=-100)
    catalog.sources.append(previous)
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is previous
    catalog.sources.append(preferred)

    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is preferred
    assert next(iter(selection._affinity.values()))[0] == preferred.id


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", POLICIES)
async def test_affinity_stays_ahead_of_priority_within_winning_policy(catalog: _Catalog, policy: str) -> None:
    previous = _source("previous", policy)
    catalog.sources.append(previous)
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is previous
    higher_priority = _source("higher-priority", policy, priority=100)
    catalog.sources.append(higher_priority)

    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is previous
    assert await selection.select_model_source(MODEL, None, protocol="messages") is higher_priority


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", POLICIES)
async def test_disabled_source_is_excluded_even_with_affinity(catalog: _Catalog, policy: str) -> None:
    paused = _source("paused", policy, priority=100)
    fallback = _source("fallback", "preserve", priority=-100)
    catalog.sources.append(paused)
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is paused
    paused.is_enabled = False
    catalog.sources.append(fallback)

    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is fallback
    fallback.is_enabled = False
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unavailable",
    [
        "cooldown",
        "stale_quota",
        "stale_entitlement",
        "exhausted",
        "model",
        "protocol",
        "capacity",
        "excluded",
        "streaming",
    ],
)
async def test_burn_first_never_bypasses_eligibility_or_capacity(catalog: _Catalog, unavailable: str) -> None:
    burn = _source("burn", "burn_first", priority=100)
    preserved = _source("preserved", "preserve")
    catalog.sources[:] = [burn, preserved]
    if unavailable == "cooldown":
        burn.cooldown_until = NOW + timedelta(minutes=1)
    elif unavailable == "stale_quota":
        burn.quota_checked_at = NOW - timedelta(minutes=6)
    elif unavailable == "stale_entitlement":
        burn.entitlement_verified_at = NOW - timedelta(minutes=6)
    elif unavailable == "exhausted":
        burn.quota_windows[0].used_ratio = 1
    elif unavailable == "model":
        burn.models[0].is_enabled = False
    elif unavailable == "protocol":
        burn.supports_messages = False
    elif unavailable == "capacity":
        catalog.counts[burn.id] = 1
    elif unavailable == "streaming":
        burn.models[0].supports_streaming = False

    assert (
        await selection.select_model_source(
            MODEL,
            None,
            protocol="messages",
            require_streaming=True,
            affinity_key="thread",
            exclude_source_ids={burn.id} if unavailable == "excluded" else None,
        )
        is preserved
    )


@pytest.mark.asyncio
async def test_preserve_becomes_available_only_after_all_preferred_sources_are_unavailable(catalog: _Catalog) -> None:
    burn, normal, preserved = [_source(policy, policy) for policy in POLICIES]
    catalog.sources[:] = [preserved, normal, burn]
    assert await selection.select_model_source(MODEL, None, protocol="messages") is burn
    burn.is_enabled = False
    assert await selection.select_model_source(MODEL, None, protocol="messages") is normal
    normal.cooldown_until = NOW + timedelta(minutes=1)
    assert await selection.select_model_source(MODEL, None, protocol="messages") is preserved


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, "unknown"])
async def test_unset_or_unknown_policy_has_native_normal_semantics(catalog: _Catalog, policy: str | None) -> None:
    normal = _source("normal")
    normal.routing_policy = policy
    catalog.sources[:] = [_source("preserved", "preserve", priority=100), normal]
    assert await selection.select_model_source(MODEL, None, protocol="messages") is normal
    burn = _source("burn", "burn_first", priority=-100)
    catalog.sources.append(burn)
    assert await selection.select_model_source(MODEL, None, protocol="messages") is burn


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("protocol", ["messages", "chat_completions", "responses"])
def test_stale_selected_snapshot_rejects_currently_paused_source(policy: str, protocol: str) -> None:
    selected = _source("selected", policy)
    current = _source("selected", policy)
    # Snapshot comparisons use the database's naive UTC timestamps.
    selected.entitlement_verified_at = current.entitlement_verified_at = NOW.replace(tzinfo=None)
    selected.quota_checked_at = current.quota_checked_at = NOW.replace(tzinfo=None)
    selected.supports_chat_completions = current.supports_chat_completions = True
    selected.supports_responses = current.supports_responses = True
    subscription_runtime._assert_selected_snapshot(selected, current, protocol)
    current.is_enabled = False

    with pytest.raises(ProviderError, match="configuration changed after selection"):
        subscription_runtime._assert_selected_snapshot(selected, current, protocol)
    assert selected.is_enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_type", ["claude_oauth", "api_key"])
async def test_upstream_headers_reject_pause_after_selection_before_reading_secret(
    monkeypatch: pytest.MonkeyPatch, auth_type: str
) -> None:
    selected = _source("selected", "burn_first")
    current = _source("selected", "burn_first")
    selected.auth_type = current.auth_type = auth_type
    if auth_type == "api_key":
        selected.api_key_encrypted = current.api_key_encrypted = b"encrypted"
    current.is_enabled = False

    async def load_current(source_id: str) -> ModelSource:
        assert source_id == selected.id
        return current

    def unexpected_decryption() -> None:
        pytest.fail("paused source must be rejected before constructing an encryptor")

    monkeypatch.setattr(subscription_runtime, "fresh_credential", load_current)
    monkeypatch.setattr(subscription_runtime, "load_source", load_current)
    monkeypatch.setattr(subscription_runtime, "TokenEncryptor", unexpected_decryption)

    with pytest.raises(ProviderError, match="configuration changed after selection"):
        await subscription_runtime.upstream_headers(selected, protocol="messages")


def test_idle_reset_observation_requires_explicit_warmup_opt_in() -> None:
    source = _source("idle")
    window = source.quota_windows[0]
    window.window_key = "five_hour"
    window.used_ratio = 0
    window.resets_at = NOW - timedelta(minutes=1)

    assert not selection.source_is_eligible(source, None, MODEL, protocol="messages")
    assert selection.source_is_eligible(source, None, MODEL, protocol="messages", allow_idle_five_hour=True)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("resets_at", None, True),
        ("resets_at", NOW, True),
        ("resets_at", NOW + timedelta(seconds=1), False),
        ("observed_at", NOW - timedelta(minutes=2), False),
        ("observed_at", NOW - timedelta(minutes=6), False),
        ("observed_at", NOW + timedelta(seconds=31), False),
        ("used_ratio", 0.01, False),
        ("used_ratio", None, False),
        ("used_ratio", float("nan"), False),
        ("used_value", 0, True),
        ("used_value", 1, False),
        ("used_value", -1, False),
        ("used_value", float("nan"), False),
        ("window_key", "seven_day", False),
        ("model", MODEL, False),
    ],
)
def test_idle_five_hour_requires_fresh_shared_zero_usage_after_reset(field, value, expected: bool) -> None:
    source = _source("idle")
    window = source.quota_windows[0]
    window.window_key = "five_hour"
    window.used_ratio = 0
    window.resets_at = NOW - timedelta(minutes=1)
    setattr(window, field, value)

    assert selection._idle_five_hour(window, NOW) is expected


@pytest.mark.parametrize("used_ratio", [None, 0.1, 1.0])
def test_warmup_does_not_infer_idle_from_clock_crossing(used_ratio: float | None) -> None:
    source = _source("idle")
    window = source.quota_windows[0]
    window.window_key = "five_hour"
    window.used_ratio = used_ratio
    window.resets_at = NOW - timedelta(minutes=1)

    assert not selection.source_is_eligible(source, None, MODEL, protocol="messages", allow_idle_five_hour=True)


@pytest.mark.parametrize("failure", ["exhausted", "stale", "reset", "unknown"])
def test_warmup_idle_exception_keeps_other_windows_fail_closed(failure: str) -> None:
    source = _source("idle")
    source.quota_windows.append(
        ModelSourceQuotaWindow(
            window_key="five_hour", used_ratio=0, observed_at=NOW, resets_at=NOW - timedelta(minutes=1)
        )
    )
    weekly = source.quota_windows[0]
    if failure == "exhausted":
        weekly.used_ratio = 1
    elif failure == "stale":
        weekly.observed_at = NOW - timedelta(minutes=6)
    elif failure == "reset":
        weekly.resets_at = NOW - timedelta(seconds=1)
    else:
        weekly.used_ratio = None

    assert not selection.source_is_eligible(source, None, MODEL, protocol="messages", allow_idle_five_hour=True)


def test_warmup_idle_exception_cannot_bypass_shared_unresolved_exhaustion() -> None:
    source = _source("idle")
    peer = _source("exhausted-peer")
    peer.oauth_credential.account_uuid = source.oauth_credential.account_uuid
    for row in (source, peer):
        row.quota_windows[0].window_key = "five_hour"
    source.quota_windows[0].used_ratio = 0
    source.quota_windows[0].resets_at = NOW - timedelta(minutes=1)
    peer.quota_windows[0].used_ratio = 1
    peer.quota_windows[0].observed_at = NOW - timedelta(minutes=2)
    peer.quota_windows[0].resets_at = NOW + timedelta(hours=1)

    assert not selection.source_is_eligible(
        source, None, MODEL, protocol="messages", allow_idle_five_hour=True, group_sources=[source, peer]
    )


@pytest.mark.parametrize("allow_idle_five_hour", [False, True])
@pytest.mark.parametrize(
    "age,expected",
    [
        (timedelta(minutes=5), True),
        (timedelta(minutes=5, microseconds=1), False),
        (timedelta(seconds=-30), True),
        (timedelta(seconds=-31), False),
    ],
)
def test_plan_entitlement_must_be_fresh_even_with_fresh_quota(
    age: timedelta, expected: bool, allow_idle_five_hour: bool
) -> None:
    source = _source("fresh-quota")
    source.entitlement_verified_at = NOW - age

    assert (
        selection.source_is_eligible(
            source, None, MODEL, protocol="messages", allow_idle_five_hour=allow_idle_five_hour
        )
        is expected
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_enabled", False),
        ("entitlement_status", "unverified"),
        ("entitlement_verified_at", None),
        ("quota_checked_at", NOW - timedelta(minutes=6)),
        ("quota_error", "provider unavailable"),
        ("cooldown_until", NOW + timedelta(minutes=1)),
        ("extra_usage_enabled", True),
    ],
)
def test_warmup_idle_exception_keeps_source_eligibility_fail_closed(field, value) -> None:
    source = _source("idle")
    window = source.quota_windows[0]
    window.window_key = "five_hour"
    window.used_ratio = 0
    window.resets_at = NOW - timedelta(minutes=1)
    setattr(source, field, value)

    assert not selection.source_is_eligible(source, None, MODEL, protocol="messages", allow_idle_five_hour=True)


def _api_source(source_id: str = "paid", *, model: str = MODEL, provider: str = "anthropic") -> ModelSource:
    row = _source(source_id, "burn_first", priority=1000)
    row.provider = provider
    row.billing_mode = "api_metered"
    row.auth_type = "api_key"
    row.api_key_encrypted = b"encrypted"
    row.oauth_credential = None
    row.api_fallback_only = True
    row.api_balance_is_available = None
    row.api_rate_limits = []
    row.quota_windows = []
    row.supports_chat_completions = True
    row.supports_responses = True
    row.models[0].model = f"{provider}/{model}"
    row.models[0].upstream_model = model
    return row


def _key(**changes) -> ApiKeyData:
    return replace(ApiKeyData(
        id="key", name="key", key_prefix="test", allowed_models=None, enforced_model=None,
        enforced_reasoning_effort=None, enforced_service_tier=None, expires_at=None,
        is_active=True, created_at=NOW, last_used_at=None, billing_policy="any",
    ), **changes)


async def _select(catalog: _Catalog, repository: bool) -> ModelSource | None:
    if repository:
        return await catalog.find_messages_source_for_model(MODEL)
    return await selection.select_model_source(MODEL, None, protocol="messages", require_streaming=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", [False, True])
async def test_eligible_subscription_beats_fallback_api_burn_priority(catalog: _Catalog, repository: bool) -> None:
    plan = _source("plan", "preserve", priority=-1000)
    paid = _api_source()
    catalog.sources[:] = [paid, plan]

    assert await _select(catalog, repository) is plan


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", [False, True])
@pytest.mark.parametrize("unavailable", [
    "disabled", "exhausted", "cooldown", "capacity", "stale", "entitlement", "model", "protocol", "streaming",
])
async def test_api_fallback_after_subscription_eligibility_and_capacity(
    catalog: _Catalog, repository: bool, unavailable: str,
) -> None:
    plan, paid = _source("plan"), _api_source()
    catalog.sources[:] = [plan, paid]
    if unavailable == "disabled":
        plan.is_enabled = False
    elif unavailable == "exhausted":
        plan.quota_windows[0].used_ratio = 1
    elif unavailable == "cooldown":
        plan.cooldown_until = NOW + timedelta(minutes=1)
    elif unavailable == "capacity":
        catalog.counts[plan.id] = 1
    elif unavailable == "stale":
        plan.quota_checked_at = NOW - timedelta(minutes=6)
    elif unavailable == "entitlement":
        plan.entitlement_status = "unverified"
    elif unavailable == "model":
        plan.models[0].is_enabled = False
    elif unavailable == "protocol":
        plan.supports_messages = False
    else:
        plan.models[0].supports_streaming = False

    if repository:
        assert await catalog.find_messages_source_for_model(MODEL, require_streaming=True) is paid
    else:
        assert await _select(catalog, repository) is paid


@pytest.mark.asyncio
async def test_recovered_subscription_preempts_paid_affinity(catalog: _Catalog) -> None:
    plan, paid = _source("plan", "preserve", priority=-1000), _api_source()
    plan.is_enabled = False
    catalog.sources[:] = [plan, paid]
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is paid
    plan.is_enabled = True

    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="thread") is plan
    assert next(iter(selection._affinity.values()))[0] == plan.id


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", [False, True])
async def test_api_opt_out_competes_with_subscriptions_normally(catalog: _Catalog, repository: bool) -> None:
    plan, paid = _source("plan"), _api_source()
    paid.api_fallback_only = False
    catalog.sources[:] = [plan, paid]

    assert await _select(catalog, repository) is paid


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["custom", "openai", "deepseek", "nanogpt", "kimi", "zai"])
@pytest.mark.parametrize("requested", ["claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"])
async def test_trusted_claude_request_does_not_match_other_supplier_alias(
    catalog: _Catalog, provider: str, requested: str,
) -> None:
    impostor = _api_source(provider=provider, model="claude-sonnet-4-6")
    impostor.models[0].model = requested
    catalog.sources[:] = [impostor]

    assert await selection.select_model_source(requested, None, protocol="messages") is None
    assert not await selection.model_is_source_owned(requested, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"])
async def test_messages_only_provider_is_owned_on_responses_transport(catalog: _Catalog, requested: str) -> None:
    paid = _api_source(model="claude-sonnet-4-6")
    paid.supports_responses = False
    catalog.sources[:] = [paid]

    assert await selection.select_responses_model_source(requested, None) is None
    assert await selection.responses_model_is_source_owned(requested, None)
    assert not await selection.responses_model_is_source_owned("gpt-5.4", None)


@pytest.mark.asyncio
async def test_claude_native_maps_exact_anthropic_upstream(catalog: _Catalog) -> None:
    native = "claude-sonnet-4-6"
    plan, paid = _source("claude"), _api_source(model=native)
    plan.models[0].model = native
    plan.quota_windows[0].used_ratio = 1
    catalog.sources[:] = [plan, paid]
    key = _key(allowed_models=[native], enforced_model=native)

    assert await selection.select_model_source(native, key, protocol="messages") is paid
    assert selection.selected_model_entry(paid, native) is paid.models[0]
    # Native operation admission revalidates the original ACL model, not the alias.
    assert selection.source_is_eligible(paid, key, native, protocol="messages", group_sources=catalog.sources)
    assert not selection.source_is_eligible(paid, key, paid.models[0].model, protocol="messages")
    paid.models[0].upstream_model = "claude-opus-4-6"
    assert await selection.select_model_source(native, key, protocol="messages") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"billing_policy": "subscription_only"},
    {"source_assignment_scope_enabled": True, "assigned_source_ids": []},
    {"source_assignment_scope_enabled": True, "assigned_source_ids": ["other"]},
    {"allowed_models": ["anthropic/claude-sonnet-4-6"]},
    {"enforced_model": "other"},
    {"is_active": False},
    {"expires_at": NOW},
])
async def test_native_api_fallback_cannot_bypass_key_controls(catalog: _Catalog, changes: dict) -> None:
    model = "claude-sonnet-4-6"
    catalog.sources[:] = [_api_source(model=model)]

    assert await selection.select_model_source(model, _key(**changes), protocol="messages") is None


@pytest.mark.asyncio
async def test_explicit_paid_assignment_does_not_require_unassigned_subscription(catalog: _Catalog) -> None:
    plan, paid = _source("plan"), _api_source()
    catalog.sources[:] = [plan, paid]
    key = _key(source_assignment_scope_enabled=True, assigned_source_ids=[paid.id], allowed_models=[MODEL])

    assert await selection.select_model_source(MODEL, key, protocol="messages") is paid


@pytest.mark.asyncio
async def test_explicit_alias_preserves_upstream_override_and_alias_acl(catalog: _Catalog) -> None:
    paid = _api_source(model="gpt-5.4", provider="openai")
    paid.models[0].model = "openai/team-model"
    catalog.sources[:] = [paid]
    key = _key(allowed_models=["openai/team-model"], enforced_model="openai/team-model")

    assert await selection.select_responses_model_source("openai/team-model", key) == (paid, "openai/team-model")
    assert await selection.responses_model_is_source_owned("openai/team-model", key)


@pytest.mark.asyncio
@pytest.mark.parametrize("assigned", [False, True])
async def test_registry_native_stays_codex_default_even_with_source_assignment(
    catalog: _Catalog, assigned: bool,
) -> None:
    paid = _api_source(model="gpt-5.4", provider="openai")
    catalog.sources[:] = [paid]
    key = _key(source_assignment_scope_enabled=assigned, assigned_source_ids=[paid.id])

    assert await selection.select_model_source("gpt-5.4", key, protocol="responses") is None
    assert await selection.select_responses_model_source("gpt-5.4", key) is None
    assert not await selection.responses_model_is_source_owned("gpt-5.4", key)


@pytest.mark.asyncio
async def test_registry_native_exact_api_fallback_returns_internal_alias_not_acl_model(catalog: _Catalog) -> None:
    paid = _api_source(model="gpt-5.4", provider="openai")
    catalog.sources[:] = [paid]
    key = _key(allowed_models=["gpt-5.4"], enforced_model="gpt-5.4")

    assert await selection.select_model_source("gpt-5.4", key, protocol="responses", codex_fallback_tier=3) is paid
    assert await selection.select_responses_model_source("gpt-5.4", key, codex_fallback_tier=3) == (
        paid, "openai/gpt-5.4",
    )
    assert selection.selected_model_entry(paid, "gpt-5.4") is paid.models[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,upstream", [
    ("openai", "gpt-5.4-mini"), ("openai", "gpt-5.4-2026-10-01"),
    ("custom", "gpt-5.4"), ("nanogpt", "gpt-5.4"), ("anthropic", "gpt-5.4"),
])
async def test_gpt_api_fallback_requires_exact_openai_catalog_identity(
    catalog: _Catalog, provider: str, upstream: str | None,
) -> None:
    paid = _api_source(model="gpt-5.4", provider=provider)
    # Even a matching public alias cannot invent the upstream catalog identity.
    paid.models[0].model = "gpt-5.4"
    paid.models[0].upstream_model = upstream
    catalog.sources[:] = [paid]

    assert await selection.select_responses_model_source("gpt-5.4", None, codex_fallback_tier=3) is None
    assert not await selection.responses_model_is_source_owned("gpt-5.4", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"billing_policy": "subscription_only"}, {"allowed_models": ["openai/gpt-5.4"]},
    {"enforced_model": "openai/gpt-5.4"},
    {"source_assignment_scope_enabled": True, "assigned_source_ids": ["other"]},
])
async def test_gpt_api_fallback_preserves_native_key_acl(catalog: _Catalog, changes: dict) -> None:
    catalog.sources[:] = [_api_source(model="gpt-5.4", provider="openai")]

    assert await selection.select_responses_model_source("gpt-5.4", _key(**changes), codex_fallback_tier=3) is None


@pytest.mark.asyncio
async def test_registry_model_messages_retains_subscription_compatibility(catalog: _Catalog) -> None:
    plan = _source("nano")
    plan.provider = "nanogpt"
    plan.models[0].model = "gpt-5.4"
    catalog.sources[:] = [plan]

    assert await selection.select_model_source("gpt-5.4", None, protocol="messages") is plan


@pytest.mark.asyncio
async def test_paid_fallback_keeps_balance_and_capacity_fences(catalog: _Catalog) -> None:
    paid = _api_source()
    catalog.sources[:] = [paid]
    paid.api_balance_is_available = False
    assert await selection.select_model_source(MODEL, None, protocol="messages") is None
    paid.api_balance_is_available = None
    catalog.counts[paid.id] = 1
    assert await selection.select_model_source(MODEL, None, protocol="messages") is None
    catalog.counts.clear()
    assert await selection.select_model_source(MODEL, None, protocol="messages") is paid


@pytest.mark.asyncio
async def test_native_api_streaming_checks_matched_entry(catalog: _Catalog) -> None:
    paid = _api_source(model="gpt-5.4", provider="openai")
    paid.models[0].supports_streaming = False
    catalog.sources[:] = [paid]

    assert await selection.select_responses_model_source(
        "gpt-5.4", None, require_streaming=True, codex_fallback_tier=3,
    ) is None


@pytest.mark.asyncio
async def test_same_tier_api_prefers_fresh_observed_headroom(catalog: _Catalog) -> None:
    from app.db.models import ModelSourceApiRateLimit

    low, high = _api_source("a-low"), _api_source("z-high")
    for source, remaining in ((low, 10), (high, 90)):
        source.api_rate_limits = [ModelSourceApiRateLimit(
            model=MODEL, token_kind="tokens", observed_at=NOW,
            token_limit=100, tokens_remaining=remaining, tokens_reset_at=NOW + timedelta(minutes=1),
        )]
    catalog.sources[:] = [low, high]
    assert await selection.select_model_source(MODEL, None, protocol="messages") is high


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["rate", "cooldown"])
async def test_api_affinity_never_bypasses_observed_rate_or_cooldown(catalog: _Catalog, blocked: str) -> None:
    from app.db.models import ModelSourceApiRateLimit

    first, second = _api_source("first"), _api_source("second")
    catalog.sources[:] = [first]
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="api-thread") is first
    catalog.sources.append(second)
    if blocked == "rate":
        first.api_rate_limits = [ModelSourceApiRateLimit(
            model=MODEL, token_kind="tokens", observed_at=NOW,
            token_limit=100, tokens_remaining=0, tokens_reset_at=NOW + timedelta(minutes=1),
        )]
    else:
        first.cooldown_until = NOW + timedelta(minutes=1)
    assert await selection.select_model_source(MODEL, None, protocol="messages", affinity_key="api-thread") is second


@pytest.mark.asyncio
async def test_stale_api_observation_is_neutral_not_invented_exhaustion(catalog: _Catalog) -> None:
    from app.db.models import ModelSourceApiRateLimit

    source = _api_source()
    source.api_rate_limits = [ModelSourceApiRateLimit(
        model=MODEL, token_kind="tokens", observed_at=NOW - timedelta(minutes=6),
        token_limit=100, tokens_remaining=0,
    )]
    catalog.sources[:] = [source]
    assert await selection.select_model_source(MODEL, None, protocol="messages") is source


@pytest.mark.asyncio
async def test_exact_native_catalog_entry_needs_no_alias_override(catalog: _Catalog) -> None:
    source = _api_source(model="gpt-5.4", provider="openai")
    source.models[0].model = "gpt-5.4"
    source.models[0].upstream_model = None
    catalog.sources[:] = [source]
    assert await selection.select_responses_model_source("gpt-5.4", None, codex_fallback_tier=3) == (
        source, "gpt-5.4",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_only", [True, False])
@pytest.mark.parametrize("native_tier", [0, 1, 2, 3])
@pytest.mark.parametrize("api_policy", POLICIES)
async def test_native_api_participation_respects_opt_out_policy_and_subscription_ties(
    catalog: _Catalog, fallback_only: bool, native_tier: int, api_policy: str,
) -> None:
    source = _api_source(model="gpt-5.4", provider="openai")
    source.api_fallback_only = fallback_only
    source.routing_policy = api_policy
    source.priority = 999999  # Priority cannot buy entry into a losing tier.
    catalog.sources[:] = [source]
    selected = await selection.select_responses_model_source(
        "gpt-5.4", _key(allowed_models=["gpt-5.4"]),
        require_streaming=True, codex_fallback_tier=native_tier,
    )
    api_wins = native_tier == 3 or not fallback_only and POLICIES.index(api_policy) < native_tier
    assert selected == ((source, "openai/gpt-5.4") if api_wins else None)


@pytest.mark.asyncio
async def test_native_opt_out_ranks_only_participating_apis_by_existing_priority(catalog: _Catalog) -> None:
    fallback = _api_source("fallback", model="gpt-5.4", provider="openai")
    fallback.priority = 1000000
    first = _api_source("first", model="gpt-5.4", provider="openai")
    second = _api_source("second", model="gpt-5.4", provider="openai")
    for source in (first, second):
        source.api_fallback_only = False
    first.priority, second.priority = 5, 10
    catalog.sources[:] = [fallback, first, second]
    assert await selection.select_responses_model_source("gpt-5.4", None, codex_fallback_tier=1) == (
        second, "openai/gpt-5.4",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["balance", "capacity", "protocol", "streaming", "scope", "subscription_only"])
async def test_native_opt_out_does_not_bypass_api_eligibility(catalog: _Catalog, constraint: str) -> None:
    source = _api_source(model="gpt-5.4", provider="openai")
    source.api_fallback_only = False
    key = _key()
    if constraint == "balance":
        source.api_balance_is_available = False
    elif constraint == "capacity":
        catalog.counts[source.id] = 1
    elif constraint == "protocol":
        source.supports_responses = False
    elif constraint == "streaming":
        source.models[0].supports_streaming = False
    elif constraint == "scope":
        key = _key(source_assignment_scope_enabled=True, assigned_source_ids=["unrelated"])
    else:
        key = _key(billing_policy="subscription_only")
    catalog.sources[:] = [source]
    assert await selection.select_responses_model_source(
        "gpt-5.4", key, require_streaming=True, codex_fallback_tier=1,
    ) is None


@pytest.mark.asyncio
async def test_native_opt_out_uses_existing_api_headroom_within_policy_tier(catalog: _Catalog) -> None:
    from app.db.models import ModelSourceApiRateLimit

    low = _api_source("a-low", model="gpt-5.4", provider="openai")
    high = _api_source("z-high", model="gpt-5.4", provider="openai")
    for source, remaining in ((low, 10), (high, 90)):
        source.api_fallback_only = False
        source.api_rate_limits = [ModelSourceApiRateLimit(
            model="gpt-5.4", token_kind="tokens", observed_at=NOW,
            token_limit=100, tokens_remaining=remaining, tokens_reset_at=NOW + timedelta(minutes=1),
        )]
    catalog.sources[:] = [low, high]
    assert await selection.select_responses_model_source("gpt-5.4", None, codex_fallback_tier=1) == (
        high, "openai/gpt-5.4",
    )
