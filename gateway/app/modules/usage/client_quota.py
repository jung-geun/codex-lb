"""Read-only model-scoped subscription observations, never routing authority."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import SQLColumnExpression

from app.core.openai.model_registry import get_model_registry
from app.db.models import (
    Account,
    AccountStatus,
    AdditionalUsageHistory,
    ModelSource,
    ModelSourceQuotaWindow,
    UsageHistory,
)
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.selection import (
    QUOTA_MAX_AGE,
    _window_exhausted,
    selected_model_entry,
    source_is_eligible,
)
from app.modules.proxy._load_balancer.model_eligibility import (
    _additional_quota_applies_to_plan,
    _filter_accounts_for_model_with_catalog_evidence,
)
from app.modules.proxy.additional_model_limits import get_additional_quota_key_for_model_id
from app.modules.proxy.request_policy import resolve_model_alias
from app.modules.quota_estimates.batch import SourceIdentity, native_window_key, source_identity_groups
from app.modules.usage.additional_quota_keys import get_additional_quota_query_scope
from app.modules.usage.repository import _window_clause


class ClientQuotaWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: str
    remaining_account_equivalents: float | None
    reporting_pools: int
    unknown_pools: int
    remaining_percent: float | None
    observed_at: datetime | None
    resets_at: datetime | None


class ClientQuotaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: Literal["codex", "claude"] | None
    generated_at: datetime
    hidden: bool = False
    windows: list[ClientQuotaWindow] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QuotaSample:
    remaining: float | None = None
    observed_at: datetime | None = None
    resets_at: datetime | None = None


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fresh(value: datetime | None, now: datetime) -> bool:
    return value is not None and timedelta(seconds=-30) <= now - utc(value) <= QUOTA_MAX_AGE


def _known(sample: QuotaSample, now: datetime) -> bool:
    return (
        sample.remaining is not None
        and math.isfinite(sample.remaining)
        and 0 <= sample.remaining <= 1
        and _fresh(sample.observed_at, now)
        and (sample.resets_at is None or utc(sample.resets_at) > now)
    )


def aggregate_window(window: str, samples: list[QuotaSample], now: datetime) -> ClientQuotaWindow:
    known = [sample for sample in samples if _known(sample, now)]
    unknown = len(samples) - len(known)
    remaining = sum(sample.remaining for sample in known if sample.remaining is not None) if known else None
    return ClientQuotaWindow(
        window=window,
        remaining_account_equivalents=remaining,
        reporting_pools=len(known),
        unknown_pools=unknown,
        remaining_percent=remaining / len(known) * 100 if remaining is not None and not unknown else None,
        observed_at=min((utc(sample.observed_at) for sample in known if sample.observed_at is not None), default=None),
        resets_at=min((utc(sample.resets_at) for sample in known if sample.resets_at is not None), default=None),
    )


def _bound(samples: list[QuotaSample], now: datetime) -> QuotaSample:
    if not samples or any(not _known(sample, now) for sample in samples):
        return QuotaSample()
    return QuotaSample(
        min(sample.remaining for sample in samples if sample.remaining is not None),
        min(utc(sample.observed_at) for sample in samples if sample.observed_at is not None),
        min((utc(sample.resets_at) for sample in samples if sample.resets_at is not None), default=None),
    )


def _native_label(slot: str, minutes: int | None) -> str:
    # The upstream slot is not a duration: free plans can report weekly primary.
    if minutes is not None and minutes > 0:
        return {300: "5h", 10080: "7d", 43200: "30d"}.get(minutes, f"{minutes}m")
    return native_window_key(slot, minutes)


def _native_sample(row: UsageHistory | AdditionalUsageHistory) -> QuotaSample:
    reset = None
    if row.reset_at is not None:
        try:
            reset = datetime.fromtimestamp(row.reset_at, UTC)
        except (ValueError, OverflowError, OSError):
            return QuotaSample()
    return QuotaSample(
        max(0.0, 1 - row.used_percent / 100)
        if row.used_percent is not None and math.isfinite(row.used_percent) and row.used_percent >= 0
        else None,
        row.recorded_at,
        reset,
    )


async def _native_pools(
    session: AsyncSession,
    api_key: ApiKeyData,
    model: str,
    now: datetime,
) -> list[dict[str, QuotaSample]]:
    stmt = select(Account).where(
        Account.delete_requested_at.is_(None),
        Account.status.in_((AccountStatus.ACTIVE, AccountStatus.RATE_LIMITED, AccountStatus.QUOTA_EXCEEDED)),
    )
    if api_key.account_assignment_scope_enabled:
        stmt = stmt.where(Account.id.in_(api_key.assigned_account_ids))
    quota_key = get_additional_quota_key_for_model_id(model)
    model_filter = _filter_accounts_for_model_with_catalog_evidence(
        list((await session.scalars(stmt)).all()),
        model,
        registry=get_model_registry(),
        service_tier=api_key.enforced_service_tier,
        additional_quota_can_override_account_catalog=quota_key is not None,
    )
    accounts = model_filter.accounts
    ids = [account.id for account in accounts]
    if not ids:
        return []
    # One statement, three indexed top-one probes per authorized account. Do
    # not rank retained history: production databases retain years of samples.
    native_probes = []
    for slot in ("primary", "secondary", "monthly"):
        latest_id = (
            select(UsageHistory.id)
            .where(
                UsageHistory.account_id == Account.id,
                _window_clause(slot),
            )
            .order_by(UsageHistory.recorded_at.desc(), UsageHistory.id.desc())
            .limit(1)
            .correlate(Account)
            .scalar_subquery()
        )
        native_probes.append(select(latest_id.label("id")).select_from(Account).where(Account.id.in_(ids)))
    latest_ids = union_all(*native_probes).subquery()
    native_rows = (
        await session.scalars(select(UsageHistory).where(UsageHistory.id.in_(select(latest_ids.c.id))))
    ).all()
    rows: dict[str, dict[str, UsageHistory]] = defaultdict(dict)
    for row in native_rows:
        rows[row.window or "primary"][row.account_id] = row
    extras: dict[str, dict[str, AdditionalUsageHistory]] = defaultdict(dict)
    scope = get_additional_quota_query_scope(quota_key=quota_key) if quota_key is not None else None
    if scope is not None:
        # Match the repository's alias indexes and conservative tie ordering,
        # but batch all probes into one statement rather than one/account.
        targets: list[tuple[SQLColumnExpression[str], str]] = [
            (AdditionalUsageHistory.quota_key, value)
            for value in sorted(scope.quota_key_match_values or {scope.quota_key})
        ]
        targets.extend(
            (func.lower(AdditionalUsageHistory.limit_name), value) for value in sorted(scope.limit_name_match_values)
        )
        targets.extend(
            (func.lower(AdditionalUsageHistory.metered_feature), value)
            for value in sorted(scope.metered_feature_match_values)
        )
        extra_probes = []
        for slot in ("primary", "secondary"):
            for column, value in targets:
                latest_extra_id = (
                    select(AdditionalUsageHistory.id)
                    .where(
                        AdditionalUsageHistory.account_id == Account.id,
                        AdditionalUsageHistory.window == slot,
                        column == value,
                    )
                    .order_by(
                        AdditionalUsageHistory.recorded_at.desc(),
                        AdditionalUsageHistory.used_percent.desc(),
                        AdditionalUsageHistory.id.desc(),
                    )
                    .limit(1)
                    .correlate(Account)
                    .scalar_subquery()
                )
                extra_probes.append(select(latest_extra_id.label("id")).select_from(Account).where(Account.id.in_(ids)))
        latest_extra_ids = union_all(*extra_probes).subquery()
        extra_rows = (
            await session.scalars(
                select(AdditionalUsageHistory).where(AdditionalUsageHistory.id.in_(select(latest_extra_ids.c.id)))
            )
        ).all()
        for extra in extra_rows:
            previous = extras[extra.window].get(extra.account_id)
            if previous is None or (utc(extra.recorded_at), extra.used_percent, extra.id) > (
                utc(previous.recorded_at),
                previous.used_percent,
                previous.id,
            ):
                extras[extra.window][extra.account_id] = extra
    groups: dict[tuple[str, ...], list[Account]] = defaultdict(list)
    for account in accounts:
        identity = (
            ("seat", account.chatgpt_account_id, account.chatgpt_user_id)
            if account.chatgpt_account_id and account.chatgpt_user_id
            else ("unidentified", account.id)
        )
        groups[identity].append(account)
    pools: list[dict[str, QuotaSample]] = []
    for identity, members in groups.items():
        constraints: dict[str, list[QuotaSample]] = defaultdict(list)
        use_extra = quota_key is not None and any(
            _additional_quota_applies_to_plan(quota_key=quota_key, plan_type=account.plan_type)
            or model_filter.general_model_account_ids is not None
            and account.id not in model_filter.general_model_account_ids
            for account in members
        )
        for slot, by_account in rows.items():
            if use_extra and slot in {"primary", "secondary"}:
                continue  # Spark's model pool replaces, rather than spends, the standard pool.
            observations = [by_account[account.id] for account in members if account.id in by_account]
            latest_row = max(observations, key=lambda row: (utc(row.recorded_at), row.id), default=None)
            if latest_row is not None:
                constraints[_native_label(slot, latest_row.window_minutes)].append(_native_sample(latest_row))
        found_extra = False
        if use_extra:
            for slot, extra_by_account in extras.items():
                extra_observations = [
                    extra_by_account[account.id] for account in members if account.id in extra_by_account
                ]
                extra_row = max(extra_observations, key=lambda row: (utc(row.recorded_at), row.id), default=None)
                if extra_row is not None:
                    found_extra = True
                    constraints[_native_label(slot, extra_row.window_minutes)].append(_native_sample(extra_row))
        missing_extra = use_extra and not found_extra
        if not constraints:
            constraints = {"5h": [], "7d": []}
        pools.append(
            {
                label: _bound(samples, now) if identity[0] == "seat" and not missing_extra else QuotaSample()
                for label, samples in constraints.items()
            }
        )
    return pools


def _source_candidates(sources: list[ModelSource], api_key: ApiKeyData, model: str) -> list[ModelSource]:
    return [
        source
        for source in sources
        if source.is_enabled
        and source.entitlement_status not in {"blocked", "reauth_required"}
        and (not api_key.source_assignment_scope_enabled or source.id in api_key.assigned_source_ids)
        and (not api_key.allowed_models or model in api_key.allowed_models)
        and (entry := selected_model_entry(source, model)) is not None
        and entry.is_enabled
        and (
            source.billing_mode not in {"subscription", "coding_plan"}
            or entry.subscription_eligible
            and not entry.subscription_restricted
            and source.extra_usage_enabled is False
        )
        and (api_key.billing_policy != "subscription_only" or source.billing_mode in {"subscription", "coding_plan"})
    ]


def _group_windows(members: list[ModelSource], model: str) -> list[ModelSourceQuotaWindow]:
    aliases = {model}
    for peer in members:
        entry = selected_model_entry(peer, model)
        if entry is not None:
            aliases.add(entry.model)
            if entry.upstream_model is not None:
                aliases.add(entry.upstream_model)
    observations: dict[tuple[str, str | None], list[ModelSourceQuotaWindow]] = defaultdict(list)
    for peer in members:
        for window in peer.quota_windows:
            if window.model is None or window.model in aliases:
                observations[(window.window_key, window.model)].append(window)
    effective: list[ModelSourceQuotaWindow] = []
    for windows in observations.values():
        latest = max(windows, key=lambda window: utc(window.observed_at))
        effective.append(latest)
        # Match selection: clock passage cannot clear an exhausted observation.
        effective.extend(
            window
            for window in windows
            if window is not latest
            and (
                utc(window.observed_at) == utc(latest.observed_at)
                or _window_exhausted(window)
                and (window.resets_at is None or utc(window.resets_at) > utc(latest.observed_at))
            )
        )
    return effective


def _source_pools(
    sources: list[ModelSource],
    candidates: list[ModelSource],
    api_key: ApiKeyData,
    model: str,
    now: datetime,
) -> list[dict[str, QuotaSample]]:
    # Include authorized model-less aliases in identity union, not as additional
    # capacity. They can connect two registrations of the same shared pool.
    subscriptions = [
        source
        for source in sources
        if source.is_enabled
        and source.billing_mode in {"subscription", "coding_plan"}
        and (not api_key.source_assignment_scope_enabled or source.id in api_key.assigned_source_ids)
    ]
    groups, ambiguous = source_identity_groups(
        [
            SourceIdentity(
                source.id,
                "anthropic" if source.provider == "claude" else source.provider,
                source.quota_group,
                source.oauth_credential.account_uuid if source.oauth_credential else None,
                source.oauth_credential.organization_uuid if source.oauth_credential else None,
            )
            for source in subscriptions
        ]
    )
    by_id = {source.id: source for source in subscriptions}
    visited: set[frozenset[str]] = set()
    pools: list[dict[str, QuotaSample]] = []
    for source in candidates:
        if source.id not in groups:
            continue  # API token windows/balances never constitute subscription capacity.
        group = groups[source.id]
        if group in visited:
            continue
        visited.add(group)
        members = [by_id[source_id] for source_id in group]
        identified = all(
            bool(peer.quota_group or peer.oauth_credential and peer.oauth_credential.account_uuid) for peer in members
        )
        valid = identified and not bool(group.intersection(ambiguous))
        # Preserve stale/errors as unknown; exhausted fresh pools remain visible.
        valid = valid and all(
            not peer.quota_error
            and peer.entitlement_status == "verified"
            and _fresh(peer.quota_checked_at, now)
            and _fresh(peer.entitlement_verified_at, now)
            and (peer.cooldown_until is None or utc(peer.cooldown_until) <= now)
            for peer in members
        )
        valid = valid and source_is_eligible(source, api_key, model, allow_exhausted=True, group_sources=members)
        constraints: dict[str, list[QuotaSample]] = defaultdict(list)
        for window in _group_windows(members, model):
            label = {"five_hour": "5h", "seven_day": "7d", "monthly": "30d"}.get(window.window_key)
            if window.window_key.startswith(("seven_day_", "header:seven_day_")):
                label = "7d"
            if label is None:
                continue  # Unknown provider duration is not an invented 5h/7d window.
            ratios = [window.used_ratio] if window.used_ratio is not None else []
            if window.used_value is not None or window.limit_value is not None:
                if window.used_value is None or window.limit_value is None or window.limit_value <= 0:
                    ratios = []
                else:
                    ratios.append(window.used_value / window.limit_value)
            constraints[label].append(
                QuotaSample(
                    max(0.0, 1 - max(ratios)) if ratios and all(math.isfinite(ratio) for ratio in ratios) else None,
                    window.observed_at,
                    window.resets_at,
                )
            )
        for label in ("5h", "7d"):
            constraints.setdefault(label, [])
        pools.append(
            {label: _bound(samples, now) if valid else QuotaSample() for label, samples in constraints.items()}
        )
    return pools


async def build_client_quota(
    session: AsyncSession,
    api_key: ApiKeyData,
    model: str,
    *,
    hidden: bool,
) -> ClientQuotaResponse:
    now = datetime.now(UTC)
    registry = get_model_registry()
    canonical = resolve_model_alias(model) or model
    native = canonical in registry.get_models_with_fallback() and not registry.is_suppressed_model(canonical)
    if hidden:
        # Do not query or disclose account/source inventory under a privacy fence.
        return ClientQuotaResponse(model=model, provider=None, generated_at=now, hidden=True)
    sources = await ModelSourcesRepository(session).list_sources()
    candidates = _source_candidates(sources, api_key, model)
    families = {
        "claude" if source.provider in {"claude", "anthropic"} else "codex"
        for source in candidates
        if source.provider in {"claude", "anthropic", "openai"}
    }
    if native:
        families.add("codex")
    provider: Literal["codex", "claude"] | None = None
    if len(families) == 1:
        provider = "codex" if "codex" in families else "claude"
    result = ClientQuotaResponse(model=model, provider=provider, generated_at=now)
    if provider is None:
        return result
    pools = await _native_pools(session, api_key, canonical, now) if native else []
    family_sources = [
        source
        for source in sources
        if source.provider in ({"claude", "anthropic"} if provider == "claude" else {"openai"})
    ]
    pools.extend(_source_pools(family_sources, candidates, api_key, model, now))
    labels = sorted({label for pool in pools for label in pool})
    result.windows = [
        aggregate_window(label, [pool[label] for pool in pools if label in pool], now) for label in labels
    ]
    return result
