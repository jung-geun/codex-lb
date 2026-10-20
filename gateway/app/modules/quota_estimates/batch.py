"""Batch database adapters for empirical quota estimates (no writes, no N+1).

All estimator samples come directly from retained raw observation tables,
never the chart repository's hourly decimation or folded token summaries.
One request-log query serves every quota in a batch, including warmups/errors
that may have consumed provider quota. Soft-deleted logs still constitute
usage evidence. Exact duplicate rows use the existing latest-id convention.

Identity joins use a provider-validated seat principal or an explicit
provider-scoped quota_group, never the selection layer's conservative
'unidentified-subscription' scheduling bucket. Contradictory principals in
one configured group are reported as unknown, not mixed. Historical rows do
not carry identity generations: the source mutation layer must invalidate
observations when identity/group changes (as credential replacement does).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import usage as usage_core
from app.db.models import (
    Account,
    AdditionalUsageHistory,
    ModelSource,
    ModelSourceModel,
    ModelSourceOAuthCredential,
    ModelSourceQuotaObservation,
    ModelSourceQuotaWindow,
    RequestLog,
    UsageHistory,
)
from app.modules.quota_estimates.estimator import RETENTION, Observation, RequestUsage, estimate_quota, utc
from app.modules.quota_estimates.schemas import AccountQuotaEstimates, QuotaEstimatesResponse, SourceQuotaEstimates
from app.modules.usage.additional_quota_keys import (
    canonicalize_additional_quota_key,
    get_additional_quota_definition_for_key,
)

# Explicit globally covered windows emitted by current provider adapters.
# A missing model on an arbitrary new key is NOT evidence of global scope.
GLOBAL_SOURCE_WINDOWS = frozenset(
    {
        "five_hour",
        "seven_day",
        "monthly",
        "dailyInputTokens",
        "weeklyInputTokens",
        "tokens",
    }
)


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    source_id: str
    provider: str
    quota_group: str | None = None
    account_uuid: str | None = None
    organization_uuid: str | None = None


def source_identity_groups(
    sources: Sequence[SourceIdentity],
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """Transitive aliases, with conflicting proven principals fail-closed."""
    parents = {source.source_id: source.source_id for source in sources}

    def root(key: str) -> str:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    seen: dict[tuple[str, ...], str] = {}
    unidentified: dict[str, list[str]] = defaultdict(list)
    for source in sources:
        tokens: list[tuple[str, ...]] = []
        if source.account_uuid:
            tokens.append(("principal", source.provider, source.organization_uuid or "", source.account_uuid))
        if source.quota_group:
            tokens.append(("group", source.provider, source.quota_group))
        if not tokens:
            unidentified[source.provider].append(source.source_id)
        for token in tokens:
            previous = seen.setdefault(token, source.source_id)
            parents[root(source.source_id)] = root(previous)
    members: dict[str, set[str]] = defaultdict(set)
    principals: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for source in sources:
        key = root(source.source_id)
        members[key].add(source.source_id)
        if source.account_uuid:
            principals[key].add((source.provider, source.organization_uuid or "", source.account_uuid))
    ambiguous = set()
    for key, values in principals.items():
        if len(values) > 1:
            ambiguous.update(members[key])
    # With multiple unidentifiable rows, aliases cannot be ruled out. Do not
    # count just one row's calls against a potentially shared provider ratio.
    for ids in unidentified.values():
        if len(ids) > 1:
            ambiguous.update(ids)
    return ({source.source_id: frozenset(members[root(source.source_id)]) for source in sources}, frozenset(ambiguous))


def _reset(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _request_usage(row, aliases: dict[str, str] | None = None) -> RequestUsage:
    # Live callers date logs at terminal insertion, NOT request start. See
    # request_logs/repository.py:add_log and accounts/usage_rollup.py:FOLD_LAG.
    end = utc(row.requested_at)
    duration = row.latency_ms
    start = end - timedelta(milliseconds=duration) if duration is not None and duration >= 0 else None
    if row.served_model:
        actual_model = row.served_model
    else:
        actual_model = aliases.get(row.model) if aliases is not None else row.model or None
    return RequestUsage(
        started_at=start,
        completed_at=end,
        model=actual_model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        success=row.status == "success",
        cached_input_tokens=row.cached_input_tokens,
        cache_creation_input_tokens=row.cache_creation_input_tokens,
        reasoning_tokens=row.reasoning_tokens,
    )


def source_scope_models(scope: str, aliases: Sequence[dict[str, str]]) -> frozenset[str]:
    """Resolve an exact scoped ID without joining reused public alias names."""
    targets = {mapping[scope] for mapping in aliases if scope in mapping}
    if any(scope in mapping.values() for mapping in aliases):
        targets.add(scope)
    if len(targets) > 1:
        return frozenset()
    return frozenset(targets or {scope})


def native_window_key(window: str | None, minutes: int | None) -> str:
    """Use the same monthly/weekly-primary slot semantics as native usage UI."""
    slot = {"5h": "primary", "7d": "secondary", "30d": "monthly"}.get(window, window) or "primary"
    if slot in {"primary", "secondary"} and minutes == usage_core.default_window_minutes("monthly"):
        return "monthly"
    if slot == "primary" and usage_core.is_weekly_window_minutes(minutes):
        return "secondary"
    return slot


async def estimate_quotas_batch(
    session: AsyncSession,
    *,
    now: datetime,
    account_ids: Sequence[str] | None = None,
    include_sources: bool = True,
) -> QuotaEstimatesResponse:
    """Read authorized native accounts + all sources in constant query count.

    ``account_ids=None`` means all non-deleting accounts; ``[]`` means none.
    The HTTP adapter enforces accounts:read at ALL scope, as existing dashboard
    account endpoints do. Internal callers supplying restricted IDs must not
    reuse these estimates for entitlement or routing. Official Codex credits
    and all provider units are untouched; tokens here are additional metadata.
    """
    now = utc(now)
    floor = (now - RETENTION).replace(tzinfo=None)
    account_stmt = select(Account.id, Account.chatgpt_account_id, Account.chatgpt_user_id).where(
        Account.delete_requested_at.is_(None)
    )
    if account_ids is not None:
        account_stmt = account_stmt.where(Account.id.in_(account_ids))
    accounts = (await session.execute(account_stmt.order_by(Account.id))).all()
    account_members: dict[tuple[str, ...], set[str]] = defaultdict(set)
    account_group: dict[str, tuple[str, ...]] = {}
    for row in accounts:
        key = (
            ("seat", row.chatgpt_account_id, row.chatgpt_user_id)
            if row.chatgpt_account_id and row.chatgpt_user_id
            else ("row", row.id)
        )
        account_group[row.id] = key
        account_members[key].add(row.id)
    ids = [row.id for row in accounts]
    native_rows = (
        (
            await session.execute(
                select(
                    UsageHistory.account_id,
                    UsageHistory.window,
                    UsageHistory.used_percent,
                    UsageHistory.reset_at,
                    UsageHistory.recorded_at,
                    UsageHistory.window_minutes,
                ).where(UsageHistory.account_id.in_(ids), UsageHistory.recorded_at >= floor)
            )
        ).all()
        if ids
        else []
    )
    extra_rows = (
        (
            await session.execute(
                select(
                    AdditionalUsageHistory.account_id,
                    AdditionalUsageHistory.quota_key,
                    AdditionalUsageHistory.limit_name,
                    AdditionalUsageHistory.metered_feature,
                    AdditionalUsageHistory.window,
                    AdditionalUsageHistory.used_percent,
                    AdditionalUsageHistory.reset_at,
                    AdditionalUsageHistory.recorded_at,
                    AdditionalUsageHistory.window_minutes,
                ).where(AdditionalUsageHistory.account_id.in_(ids), AdditionalUsageHistory.recorded_at >= floor)
            )
        ).all()
        if ids
        else []
    )
    source_rows = (
        (
            await session.execute(
                select(
                    ModelSource.id,
                    ModelSource.provider,
                    ModelSource.quota_group,
                    ModelSourceOAuthCredential.account_uuid,
                    ModelSourceOAuthCredential.organization_uuid,
                )
                .outerjoin(ModelSourceOAuthCredential, ModelSourceOAuthCredential.source_id == ModelSource.id)
                .order_by(ModelSource.id)
            )
        ).all()
        if include_sources
        else []
    )
    source_ids = [row.id for row in source_rows]
    identities = [
        SourceIdentity(row.id, row.provider, row.quota_group, row.account_uuid, row.organization_uuid)
        for row in source_rows
    ]
    source_groups, ambiguous = source_identity_groups(identities)
    model_rows = (
        (
            await session.execute(
                select(
                    ModelSourceModel.source_id,
                    ModelSourceModel.model,
                    ModelSourceModel.upstream_model,
                ).where(ModelSourceModel.source_id.in_(source_ids))
            )
        ).all()
        if source_ids
        else []
    )
    raw_rows = (
        (
            await session.execute(
                select(
                    ModelSourceQuotaObservation.source_id,
                    ModelSourceQuotaObservation.window_key,
                    ModelSourceQuotaObservation.model,
                    ModelSourceQuotaObservation.used_ratio,
                    ModelSourceQuotaObservation.resets_at,
                    ModelSourceQuotaObservation.observed_at,
                ).where(
                    ModelSourceQuotaObservation.source_id.in_(source_ids),
                    ModelSourceQuotaObservation.observed_at >= floor,
                )
            )
        ).all()
        if source_ids
        else []
    )
    current_rows = (
        (
            await session.execute(
                select(
                    ModelSourceQuotaWindow.source_id,
                    ModelSourceQuotaWindow.window_key,
                    ModelSourceQuotaWindow.model,
                    ModelSourceQuotaWindow.used_ratio,
                    ModelSourceQuotaWindow.resets_at,
                    ModelSourceQuotaWindow.observed_at,
                ).where(ModelSourceQuotaWindow.source_id.in_(source_ids))
            )
        ).all()
        if source_ids
        else []
    )
    # Include completions after a historical sample, so calls in flight at
    # that sample invalidate the bracket instead of silently disappearing.
    log_rows = (
        (
            await session.execute(
                select(
                    RequestLog.id,
                    RequestLog.account_id,
                    RequestLog.model_source_id,
                    RequestLog.request_id,
                    RequestLog.requested_at,
                    RequestLog.latency_ms,
                    RequestLog.model,
                    RequestLog.served_model,
                    RequestLog.input_tokens,
                    RequestLog.output_tokens,
                    RequestLog.cached_input_tokens,
                    RequestLog.cache_creation_input_tokens,
                    RequestLog.reasoning_tokens,
                    RequestLog.status,
                ).where(
                    or_(RequestLog.account_id.in_(ids), RequestLog.model_source_id.in_(source_ids)),
                    RequestLog.requested_at >= floor,
                    RequestLog.requested_at <= now.replace(tzinfo=None),
                )
            )
        ).all()
        if ids or source_ids
        else []
    )
    # Same tuple as existing usage rollup plus source ownership to avoid
    # collapsing unrelated calls across provider identities. Never dedup just
    # request_id: a client may reuse it for distinct actual calls.
    deduped = {}
    for row in log_rows:
        key = (row.account_id, row.model_source_id, row.request_id, row.requested_at)
        if key not in deduped or row.id > deduped[key].id:
            deduped[key] = row
    aliases: dict[str, dict[str, str]] = defaultdict(dict)
    for row in model_rows:
        aliases[row.source_id][row.model] = row.upstream_model or row.model
    account_requests: dict[tuple[str, ...], list[RequestUsage]] = defaultdict(list)
    source_requests: dict[frozenset[str], list[RequestUsage]] = defaultdict(list)
    for row in deduped.values():
        if row.model_source_id in source_groups:
            source_requests[source_groups[row.model_source_id]].append(
                _request_usage(row, aliases[row.model_source_id])
            )
        elif row.model_source_id is None and row.account_id in account_group:
            account_requests[account_group[row.account_id]].append(_request_usage(row))
    native: dict[tuple[str, ...], dict[tuple[str, str | None], list[Observation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    scopes: dict[tuple[str, str | None], frozenset[str] | None] = {}
    for row in native_rows:
        window = native_window_key(row.window, row.window_minutes)
        native[account_group[row.account_id]][(window, None)].append(
            Observation(row.recorded_at, row.used_percent / 100, _reset(row.reset_at))
        )
    for row in extra_rows:
        quota = (
            canonicalize_additional_quota_key(
                quota_key=row.quota_key,
                limit_name=row.limit_name,
                metered_feature=row.metered_feature,
            )
            or row.quota_key
        )
        definition = get_additional_quota_definition_for_key(quota)
        key = (f"{quota}:{native_window_key(row.window, row.window_minutes)}", quota)
        scopes[key] = definition.model_ids if definition else frozenset()
        native[account_group[row.account_id]][key].append(
            Observation(row.recorded_at, row.used_percent / 100, _reset(row.reset_at))
        )
    result = QuotaEstimatesResponse()
    for account in accounts:
        group = account_group[account.id]
        windows = native[group]
        # Do not manufacture inactive 5h/weekly/monthly quotas. An entity with
        # no retained observations still gets an explicit unknown entry.
        keys = set(windows) or {("unknown", None)}
        result.accounts.append(
            AccountQuotaEstimates(
                account_id=account.id,
                shared_account_ids=sorted(account_members[group]),
                windows=[
                    estimate_quota(
                        window_key=key[0],
                        model=key[1],
                        observations=windows.get(key, []),
                        current=max(windows[key], key=lambda o: utc(o.observed_at)) if windows.get(key) else None,
                        requests=account_requests[group],
                        now=now,
                        matching_models=scopes.get(key),
                        unavailable_reason=(
                            "unsupported_scope"
                            if key[1] is None and key[0] not in {"primary", "secondary", "monthly", "unknown"}
                            else None
                        ),
                    )
                    for key in sorted(keys, key=lambda key: (key[0], key[1] or ""))
                ],
            )
        )
    source_history = defaultdict(lambda: defaultdict(list))
    source_current = defaultdict(lambda: defaultdict(list))
    for rows, target in ((raw_rows, source_history), (current_rows, source_current)):
        for row in rows:
            target[source_groups[row.source_id]][(row.window_key, row.model)].append(
                Observation(row.observed_at, row.used_ratio, row.resets_at)
            )
    for source in source_rows:
        group = source_groups[source.id]
        history, currents = source_history[group], source_current[group]
        keys = set(history) | set(currents)
        if not keys:
            keys.add(("unknown", None))
        windows = []
        known_models = {request.model for request in source_requests[group] if request.model is not None}
        for source_id in group:
            known_models.update(aliases[source_id].values())
        for key in sorted(keys, key=lambda key: (key[0], key[1] or "")):
            matching = None
            if key[1] is not None:
                matching = source_scope_models(key[1], [aliases[source_id] for source_id in group])
                if not matching.intersection(known_models):
                    matching = frozenset()
            unavailable = "identity_ambiguous" if source.id in ambiguous else None
            if unavailable is None and key[1] is None and key[0] not in GLOBAL_SOURCE_WINDOWS | {"unknown"}:
                unavailable = "unsupported_scope"
            candidates = currents.get(key) or history.get(key, [])
            windows.append(
                estimate_quota(
                    window_key=key[0],
                    model=key[1],
                    observations=history.get(key, []) + currents.get(key, []),
                    current=max(candidates, key=lambda o: utc(o.observed_at)) if candidates else None,
                    requests=source_requests[group],
                    now=now,
                    matching_models=matching,
                    unavailable_reason=unavailable,
                )
            )
        result.sources.append(
            SourceQuotaEstimates(
                source_id=source.id,
                shared_source_ids=sorted(group),
                windows=windows,
            )
        )
    return result
