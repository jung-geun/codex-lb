"""Fail-closed source eligibility, capacity and policy-aware bounded affinity.

Eligibility and capacity are resolved before fallback and routing policy tiers.
Fallback-only APIs follow matching eligible subscriptions, regardless of policy,
priority or affinity. Affinity applies only within the winning tier.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.core.openai.model_registry import get_model_registry
from app.db.models import ModelSource, ModelSourceModel, ModelSourceQuotaWindow
from app.db.session import detach_session_objects, get_background_session
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.api_usage import api_headroom_ratio, api_rate_limit_available
from app.modules.model_sources.repository import ModelSourcesRepository

SourceProtocol = Literal["messages", "chat_completions", "responses", "audio_transcriptions", "embeddings"]
QUOTA_MAX_AGE = timedelta(minutes=5)
AFFINITY_TTL_SECONDS = 300
AFFINITY_MAX_ENTRIES = 4096
_ROUTING_POLICY_RANK = {"burn_first": 0, "normal": 1, "preserve": 2}
_affinity: OrderedDict[tuple[str, str, str, str], tuple[str, float]] = OrderedDict()
_source_inflight: dict[str, int] = {}
_group_inflight: dict[str, int] = {}


_last_selected: OrderedDict[str, int] = OrderedDict()
_selection_sequence = 0


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def allowed_source_ids_for_api_key(api_key: ApiKeyData | None) -> set[str] | None:
    if api_key is None or not api_key.source_assignment_scope_enabled:
        return None
    return set(api_key.assigned_source_ids)


def source_capacity_group(source: ModelSource) -> str:
    # A provider-validated principal is stronger than an operator grouping label.
    credential = source.oauth_credential
    if credential is not None and credential.account_uuid:
        return f"{source.provider}:{credential.organization_uuid or ''}:{credential.account_uuid}"
    if source.quota_group:
        return source.quota_group
    if source.provider != "custom" and source.billing_mode in {"subscription", "coding_plan"}:
        return f"{source.provider}:unidentified-subscription"
    return source.id


def source_capacity_groups(source: ModelSource) -> tuple[str, ...]:
    principal = source_capacity_group(source)
    return (principal, f"quota:{source.quota_group}") if source.quota_group else (principal,)


def sources_share_capacity(first: ModelSource, second: ModelSource) -> bool:
    return bool(set(source_capacity_groups(first)) & set(source_capacity_groups(second)))


def _quota_usable(window: ModelSourceQuotaWindow, now: datetime, *, allow_exhausted: bool) -> bool:
    age = now - _utc(window.observed_at)
    if age < timedelta(seconds=-30) or age > QUOTA_MAX_AGE:
        return False
    # A reset is not an observation of replenishment. Require a new provider read.
    if window.resets_at is not None and _utc(window.resets_at) <= now:
        return False
    ratios: list[float] = []
    if window.used_ratio is not None:
        if not math.isfinite(window.used_ratio) or not 0 <= window.used_ratio <= 1:
            return False
        ratios.append(window.used_ratio)
    if window.limit_value is not None or window.used_value is not None:
        if window.limit_value is None or window.used_value is None:
            return False
        if not math.isfinite(window.limit_value) or not math.isfinite(window.used_value):
            return False
        if window.limit_value <= 0 or window.used_value < 0:
            return False
        ratios.append(window.used_value / window.limit_value)
    return bool(ratios) and (allow_exhausted or max(ratios) < 1)


def _idle_five_hour(window: ModelSourceQuotaWindow, now: datetime) -> bool:
    """Recognize a fresh shared idle window for warmup, never normal dispatch."""
    return (
        window.window_key == "five_hour"
        and window.model is None
        and window.used_ratio == 0
        and window.used_value in (None, 0)
        and timedelta(seconds=-30) <= now - _utc(window.observed_at) <= QUOTA_MAX_AGE
        and (
            window.resets_at is None
            or _utc(window.resets_at) <= now and _utc(window.observed_at) >= _utc(window.resets_at)
        )
    )


def _window_exhausted(window: ModelSourceQuotaWindow) -> bool:
    return (window.used_ratio is not None and math.isfinite(window.used_ratio) and window.used_ratio >= 1) or (
        window.used_value is not None
        and window.limit_value is not None
        and window.limit_value > 0
        and window.used_value >= window.limit_value
    )


def _provider_family(provider: str) -> str:
    return "anthropic" if provider == "claude" else provider


def _model_namespace(model: str) -> tuple[str | None, str]:
    namespace, separator, native = model.partition("/")
    if separator and namespace in {"openai", "anthropic", "claude", "zai", "kimi", "nanogpt", "deepseek"}:
        return _provider_family(namespace), native
    if model.startswith("claude-"):
        return "anthropic", model
    return None, model


def selected_model_entry(source: ModelSource, requested_model: str) -> ModelSourceModel | None:
    """Resolve the forwarding entry without turning its alias into an ACL model.

    Native OpenAI fallback requires an exact upstream catalog ID, not a family,
    suffix, display name or operator alias. Explicit aliases retain their own
    upstream override. Callers selecting a native model can use this helper and
    forward ``entry.model`` (whose ``upstream_model`` is applied by forwarding).
    """
    explicit = next((entry for entry in source.models if entry.model == requested_model), None)
    # An operator's explicit custom alias is not provider identity evidence.
    # Keep that legacy route, but never search a custom source for native IDs.
    if explicit is not None and source.provider in {None, "custom"} and source.billing_mode != "api_metered":
        return explicit
    family, native = _model_namespace(requested_model)
    if family is not None and _provider_family(source.provider) != family:
        return None
    registry_native = requested_model in get_model_registry().get_models_with_fallback()
    if source.billing_mode == "api_metered" and (registry_native or family is not None and native == requested_model):
        if registry_native and source.provider != "openai":
            return None
        return min(
            (entry for entry in source.models if (entry.upstream_model or entry.model) == requested_model),
            key=lambda entry: (not entry.is_enabled, entry.model != requested_model, entry.model),
            default=None,
        )
    entry = next((entry for entry in source.models if entry.model == requested_model), None)
    if entry is not None:
        return entry
    if family is not None and source.billing_mode in {"subscription", "coding_plan"}:
        return next((entry for entry in source.models if (entry.upstream_model or entry.model) == native), None)
    if source.provider in {"openai", "anthropic"} and source.billing_mode == "api_metered":
        return next((entry for entry in source.models if (entry.upstream_model or entry.model) == native), None)
    return None


def _selection_tier(source: ModelSource, candidates: Sequence[ModelSource], model: str) -> tuple[int, int]:
    entry = selected_model_entry(source, model)
    fallback = (
        source.billing_mode == "api_metered"
        and getattr(source, "api_fallback_only", True) is not False
        and any(
            peer.billing_mode in {"subscription", "coding_plan"}
            and _provider_family(peer.provider) == _provider_family(source.provider)
            and entry is not None
            and (peer_entry := selected_model_entry(peer, model)) is not None
            and (peer_entry.upstream_model or peer_entry.model) == (entry.upstream_model or entry.model)
            for peer in candidates
        )
    )
    return int(fallback), _routing_policy_rank(source)


def _effective_quota_windows(
    source: ModelSource,
    model: str,
    group_sources: Sequence[ModelSource],
) -> list[ModelSourceQuotaWindow]:
    entry = selected_model_entry(source, model)
    aliases = {model, entry.model if entry else model, entry.upstream_model if entry else model}
    observations: dict[tuple[str, str | None], list[ModelSourceQuotaWindow]] = {}
    for peer in (source, *group_sources):
        if not sources_share_capacity(source, peer):
            continue
        for window in peer.quota_windows:
            if window.model is None or window.model in aliases:
                observations.setdefault((window.window_key, window.model), []).append(window)
    effective: list[ModelSourceQuotaWindow] = []
    for windows in observations.values():
        latest = max(windows, key=lambda window: _utc(window.observed_at))
        effective.append(latest)
        # An older exhausted observation remains authoritative until a provider
        # observation actually made after its reset. A clock crossing alone is
        # never evidence of replenishment. Old auth failures are not quota.
        for window in windows:
            if window is latest or not _window_exhausted(window):
                continue
            if window.resets_at is None or _utc(window.resets_at) > _utc(latest.observed_at):
                effective.append(window)
    return effective


def _routing_policy_rank(source: ModelSource) -> int:
    return _ROUTING_POLICY_RANK.get(getattr(source, "routing_policy", "normal"), 1)


def _selection_rank(
    source: ModelSource, model: str, sources: Sequence[ModelSource]
) -> tuple[int, int, float, float, int, str]:
    windows = _effective_quota_windows(source, model, sources)
    ratios = [
        max(
            window.used_ratio or 0,
            (window.used_value / window.limit_value) if window.used_value is not None and window.limit_value else 0,
        )
        for window in windows
    ]
    resets = [_utc(window.resets_at).timestamp() for window in windows if window.resets_at is not None]
    if source.billing_mode == "api_metered":
        entry = selected_model_entry(source, model)
        ratios.append(1 - api_headroom_ratio(
            source, (entry.upstream_model or entry.model) if entry else model, now=datetime.now(UTC),
        ))
    return (
        _routing_policy_rank(source),
        -source.priority,
        round(max(ratios, default=0), 6),
        min(resets, default=float("inf")),
        _last_selected.get(source_capacity_group(source), 0),
        source.id,
    )


def _record_selection(source: ModelSource) -> None:
    global _selection_sequence
    _selection_sequence += 1
    group = source_capacity_group(source)
    _last_selected[group] = _selection_sequence
    _last_selected.move_to_end(group)
    while len(_last_selected) > AFFINITY_MAX_ENTRIES:
        _last_selected.popitem(last=False)


def source_is_eligible(
    source: ModelSource,
    api_key: ApiKeyData | None,
    model: str,
    *,
    protocol: SourceProtocol | None = None,
    allow_exhausted: bool = False,
    allow_idle_five_hour: bool = False,
    group_sources: Sequence[ModelSource] = (),
) -> bool:
    """Pure policy predicate; supply the full source snapshot for shared quota checks.

    ``allow_exhausted`` is for presenting exhausted observations, never dispatch.
    It does not override disabled, unverified, stale or unknown observations.
    ``allow_idle_five_hour`` is warmup-only: a fresh shared zero-usage five-hour
    observation may prove idle capacity after its reset, without inferring it
    from a clock crossing. Normal routing must leave this disabled.
    """
    now = datetime.now(UTC)
    if api_key is not None:
        if not api_key.is_active or (api_key.expires_at and _utc(api_key.expires_at) <= now):
            return False
        if api_key.allowed_models and model not in api_key.allowed_models:
            return False
        if api_key.enforced_model is not None and model != api_key.enforced_model:
            from app.modules.proxy.request_policy import resolve_model_alias

            if resolve_model_alias(model) != resolve_model_alias(api_key.enforced_model):
                return False
        allowed = allowed_source_ids_for_api_key(api_key)
        if allowed is not None and source.id not in allowed:
            return False
        if api_key.billing_policy not in {"any", "subscription_only"}:
            return False
    if not source.is_enabled or source.entitlement_status in {"blocked", "reauth_required"}:
        return False
    entry = selected_model_entry(source, model)
    if entry is None or not entry.is_enabled:
        return False
    capabilities = {
        "messages": source.supports_messages,
        "chat_completions": source.supports_chat_completions,
        "responses": source.supports_responses,
        "audio_transcriptions": source.supports_audio_transcriptions,
        "embeddings": source.supports_embeddings,
    }
    if protocol is not None and not capabilities.get(protocol, False):
        return False
    if source.cooldown_until is not None and _utc(source.cooldown_until) > now:
        return False
    plan = source.billing_mode in {"subscription", "coding_plan"}
    if api_key is not None and api_key.billing_policy == "subscription_only" and not plan:
        return False
    if source.billing_mode == "api_metered" and not api_rate_limit_available(
        source, entry.upstream_model or entry.model, now=now
    ):
        return False
    # Legacy custom sources remain usable under 'any', including keyless local servers.
    if source.provider != "custom" and not plan and source.billing_mode != "api_metered":
        return False
    if source.auth_type not in {"none", "api_key", "claude_oauth"}:
        return False
    if source.auth_type == "api_key" and source.api_key_encrypted is None:
        return False
    if source.auth_type == "claude_oauth" and source.oauth_credential is None:
        return False
    if (
        source.auth_type == "claude_oauth"
        and source.oauth_credential is not None
        and _utc(source.oauth_credential.expires_at) <= now
        and source.oauth_credential.refresh_token_encrypted is None
    ):
        return False
    if plan:
        if source.entitlement_status != "verified" or source.entitlement_verified_at is None:
            return False
        entitlement_age = now - _utc(source.entitlement_verified_at)
        if entitlement_age < timedelta(seconds=-30) or entitlement_age > QUOTA_MAX_AGE:
            return False
        if not entry.subscription_eligible or entry.subscription_restricted or source.extra_usage_enabled is not False:
            return False
        if source.quota_error or source.quota_checked_at is None:
            return False
        age = now - _utc(source.quota_checked_at)
        if age < timedelta(seconds=-30) or age > QUOTA_MAX_AGE:
            return False
        if any(
            peer.cooldown_until is not None and _utc(peer.cooldown_until) > now
            for peer in group_sources
            if sources_share_capacity(source, peer)
        ):
            return False
        windows = _effective_quota_windows(source, model, group_sources)
        if not windows or any(
            not _quota_usable(window, now, allow_exhausted=allow_exhausted)
            and not (allow_idle_five_hour and _idle_five_hour(window, now))
            for window in windows
        ):
            return False
    return True


def _remember_selected_capacity(source: ModelSource, peers: Sequence[ModelSource]) -> None:
    # Detached selection must carry the conservative group bound through the
    # later non-awaiting admission claim, even when another selector ran first.
    limits = [peer.max_concurrency for peer in peers
              if sources_share_capacity(source, peer) and peer.max_concurrency is not None]
    source.__dict__["_selected_group_capacity_limit"] = (
        min(limits) if limits else (1 if source.billing_mode in {"subscription", "coding_plan"} else None)
    )


def source_has_capacity(
    source: ModelSource,
    *,
    group_sources: Sequence[ModelSource] = (),
    active_operation_counts: dict[str, int] | None = None,
) -> bool:
    peers = list({peer.id: peer for peer in (source, *group_sources) if sources_share_capacity(peer, source)}.values())
    limits = [peer.max_concurrency for peer in peers if peer.max_concurrency is not None]
    group_limit = min(limits) if limits else (1 if source.billing_mode in {"subscription", "coding_plan"} else None)
    selected_limit = source.__dict__.get("_selected_group_capacity_limit")
    if selected_limit is not None:
        group_limit = min(group_limit, selected_limit) if group_limit is not None else selected_limit
    durable = active_operation_counts or {}
    source_count = max(durable.get(source.id, 0), _source_inflight.get(source.id, 0))
    group_count = max(
        sum(durable.get(peer.id, 0) for peer in peers),
        max((_group_inflight.get(group, 0) for group in source_capacity_groups(source)), default=0),
    )
    return (source.max_concurrency is None or source_count < source.max_concurrency) and (
        group_limit is None or group_count < group_limit
    )


def try_acquire_source_slot(source: ModelSource, *, group_sources: Sequence[ModelSource] = ()) -> bool:
    """Non-awaiting atomic event-loop lease; callers MUST release in finally."""
    if not source_has_capacity(source, group_sources=group_sources):
        return False
    _source_inflight[source.id] = _source_inflight.get(source.id, 0) + 1
    for group in source_capacity_groups(source):
        _group_inflight[group] = _group_inflight.get(group, 0) + 1
    return True


def release_source_slot(source: ModelSource) -> None:
    for counts, key in [
        (_source_inflight, source.id),
        *((_group_inflight, group) for group in source_capacity_groups(source)),
    ]:
        remaining = counts.get(key, 0) - 1
        if remaining > 0:
            counts[key] = remaining
        else:
            counts.pop(key, None)


async def select_model_source(
    model: str,
    api_key: ApiKeyData | None,
    *,
    protocol: SourceProtocol,
    require_streaming: bool = False,
    affinity_key: str | None = None,
    exclude_source_ids: set[str] | None = None,
    codex_fallback_tier: int | None = None,
) -> ModelSource | None:
    """Select before dispatch; native APIs must strictly beat eligible native policy."""
    registry_native = model in get_model_registry().get_models_with_fallback()
    scoped = allowed_source_ids_for_api_key(api_key) is not None
    if protocol != "messages" and registry_native and codex_fallback_tier is None and not scoped:
        return None
    async with get_background_session() as session:
        repository = ModelSourcesRepository(session)
        sources = await repository.list_sources()
        active_operations = await repository.active_operation_counts()
        candidates = [
            source
            for source in sources
            if (
                source.id not in (exclude_source_ids or ())
                and (
                    not registry_native or protocol == "messages"
                    or codex_fallback_tier is not None and source.provider == "openai"
                    and source.billing_mode == "api_metered"
                    and (
                        codex_fallback_tier == 3
                        or getattr(source, "api_fallback_only", True) is False
                        and _routing_policy_rank(source) < codex_fallback_tier
                    )
                    or codex_fallback_tier is None and scoped and source.billing_mode != "api_metered"
                )
                and source_is_eligible(source, api_key, model, protocol=protocol, group_sources=sources)
                and source_has_capacity(source, group_sources=sources, active_operation_counts=active_operations)
                and (
                    not require_streaming
                    or (entry := selected_model_entry(source, model)) is not None and entry.supports_streaming
                )
            )
        ]
        eligible = tuple(candidates)
        candidates.sort(key=lambda source: (
            _selection_tier(source, eligible, model), _selection_rank(source, model, sources)
        ))
        key = None
        now = time.monotonic()
        if affinity_key:
            key = (
                api_key.id if api_key else "anonymous",
                protocol,
                model,
                hashlib.sha256(affinity_key.encode()).hexdigest(),
            )
            previous = _affinity.get(key)
            if previous and previous[1] > now:
                candidates.sort(key=lambda source: (_selection_tier(source, eligible, model), source.id != previous[0]))
        selected = candidates[0] if candidates else None
        if selected is not None:
            _remember_selected_capacity(selected, sources)
            _record_selection(selected)
        if selected is not None and key is not None:
            _affinity[key] = (selected.id, now + AFFINITY_TTL_SECONDS)
            _affinity.move_to_end(key)
            while len(_affinity) > AFFINITY_MAX_ENTRIES:
                _affinity.popitem(last=False)
        detach_session_objects(session)
        return selected


def effective_model_for_api_key(api_key: ApiKeyData | None, requested_model: str | None) -> str | None:
    return api_key.enforced_model if api_key and api_key.enforced_model is not None else requested_model


async def select_responses_model_source(
    model: str,
    api_key: ApiKeyData | None,
    *,
    raw_model: str | None = None,
    require_streaming: bool = False,
    only_disabled: bool = False,
    codex_fallback_tier: int | None = None,
) -> tuple[ModelSource, str] | None:
    for candidate in dict.fromkeys(item for item in (raw_model, model) if item):
        if api_key is not None and api_key.allowed_models and candidate not in api_key.allowed_models:
            continue
        if api_key is not None and api_key.enforced_model is not None and candidate != api_key.enforced_model:
            from app.modules.proxy.request_policy import resolve_model_alias

            if resolve_model_alias(candidate) != resolve_model_alias(api_key.enforced_model):
                continue
        if (
            candidate in get_model_registry().get_models_with_fallback()
            and codex_fallback_tier is None and allowed_source_ids_for_api_key(api_key) is None
        ):
            continue
        if not only_disabled:
            source = await select_model_source(
                candidate, api_key, protocol="responses", require_streaming=require_streaming,
                codex_fallback_tier=codex_fallback_tier,
            )
        else:
            async with get_background_session() as session:
                source = await ModelSourcesRepository(session).find_responses_source_for_model(
                    candidate,
                    allowed_source_ids=allowed_source_ids_for_api_key(api_key),
                    require_streaming=require_streaming,
                    only_disabled=True,
                    billing_policy=api_key.billing_policy if api_key else "any",
                )
                detach_session_objects(session)
        if source is not None:
            entry = selected_model_entry(source, candidate)
            if entry is not None:
                return source, entry.model
    return None


async def model_is_source_owned(
    model: str | None,
    api_key: ApiKeyData | None,
    *,
    raw_model: str | None = None,
) -> bool:
    candidates = {item for item in (raw_model, effective_model_for_api_key(api_key, model), model) if item}
    registry = get_model_registry().get_models_with_fallback()
    async with get_background_session() as session:
        for source in await ModelSourcesRepository(session).list_sources():
            for candidate in candidates - registry.keys():
                if selected_model_entry(source, candidate) is not None:
                    # Ownership ignores health, quota and key scope: a denied
                    # source-owned request must never become a Codex attempt.
                    return True
    return False


async def responses_model_is_source_owned(
    model: str | None,
    api_key: ApiKeyData | None,
    *,
    raw_model: str | None = None,
) -> bool:
    return await model_is_source_owned(model, api_key, raw_model=raw_model)
