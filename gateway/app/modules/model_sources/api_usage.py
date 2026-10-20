"""Provider-observed API rate windows and native balances; never inferred prepaid tokens."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.core.utils.time import to_utc_naive, utcnow
from app.db.models import ModelSource, ModelSourceApiRateLimit
from app.db.session import get_background_session
from app.modules.model_sources.schemas import (
    ModelSourceApiBalanceResponse,
    ModelSourceApiRateLimitResponse,
    ModelSourceApiUsageResponse,
)

logger = logging.getLogger(__name__)
API_RATE_MAX_AGE = timedelta(minutes=5)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_DURATION_SECONDS = {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _integer(value: str | None) -> int | None:
    if value is None or len(value) > 19 or not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if number <= 2**63 - 1 else None


def _reset(value: str | None, now: datetime) -> datetime | None:
    if not value or len(value) > 128:
        return None
    # OpenAI reports durations; Anthropic reports RFC3339 timestamps.
    parts = list(_DURATION.finditer(value))
    if parts and "".join(part.group() for part in parts) == value:
        seconds = sum(float(part[1]) * _DURATION_SECONDS[part[2]] for part in parts)
        if 0 <= seconds <= 86400 * 30:
            return now + timedelta(seconds=seconds)
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return to_utc_naive(parsed) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def parse_api_rate_headers(
    provider: str, headers: Mapping[str, str], model: str, *, now: datetime | None = None,
) -> list[ModelSourceApiRateLimit]:
    """Keep input/output buckets separate. Missing/malformed values remain unknown."""
    now = to_utc_naive(now or utcnow())
    normalized = {name.lower(): value for name, value in headers.items()}
    prefix = "anthropic-ratelimit-" if provider == "anthropic" else "x-ratelimit-"

    def fields(kind: str) -> tuple[int | None, int | None, datetime | None]:
        if provider == "anthropic":
            base = prefix + kind.replace("_", "-")
            limit, remaining, reset = (
                normalized.get(base + "-" + suffix) for suffix in ("limit", "remaining", "reset")
            )
        else:
            base = prefix + "{}-" + kind.replace("_", "-")
            limit, remaining, reset = (
                normalized.get(base.format(suffix)) for suffix in ("limit", "remaining", "reset")
            )
        maximum, left = _integer(limit), _integer(remaining)
        if maximum is not None and left is not None and left > maximum:
            maximum = left = None
        return maximum, left, _reset(reset, now)

    requests = fields("requests")
    result = []
    kinds = ("tokens", "input_tokens", "output_tokens") if provider == "anthropic" else ("tokens", "project_tokens")
    for kind in kinds:
        maximum, left, reset = fields(kind)
        if maximum is None and left is None and (kind != "tokens" or requests[:2] == (None, None)):
            continue
        result.append(ModelSourceApiRateLimit(
            model=model, token_kind=kind, observed_at=now,
            token_limit=maximum, tokens_remaining=left, tokens_reset_at=reset,
            request_limit=requests[0] if kind == "tokens" else None,
            requests_remaining=requests[1] if kind == "tokens" else None,
            requests_reset_at=requests[2] if kind == "tokens" else None,
        ))
    return result


def _model_names(source: ModelSource, model: str) -> set[str]:
    names = {model}
    for entry in source.models:
        if model in {entry.model, entry.upstream_model}:
            names.update((entry.model, entry.upstream_model or entry.model))
    return names


def _current(row: ModelSourceApiRateLimit, reset_at: datetime | None, now: datetime) -> bool:
    age = now - to_utc_naive(row.observed_at)
    return -timedelta(seconds=30) <= age <= API_RATE_MAX_AGE and (
        reset_at is None or to_utc_naive(reset_at) > now
    )


def api_rate_limit_available(source: ModelSource, model: str, *, now: datetime | None = None) -> bool:
    """Fresh zero rate headroom is a refusal; unknown API quotas are not invented."""
    if getattr(source, "api_balance_is_available", None) is False:
        return False
    now = to_utc_naive(now or utcnow())
    names = _model_names(source, model)
    for row in getattr(source, "api_rate_limits", ()):
        if row.model not in names:
            continue
        for remaining, reset in (
            (row.tokens_remaining, row.tokens_reset_at),
            (row.requests_remaining, row.requests_reset_at),
        ):
            if remaining is not None and remaining <= 0 and _current(row, reset, now):
                return False
    return True


def api_headroom_ratio(source: ModelSource, model: str, *, now: datetime | None = None) -> float:
    """Lowest fresh normalized headroom across matching rate windows; larger wins."""
    now = to_utc_naive(now or utcnow())
    names = _model_names(source, model)
    ratios = []
    for row in getattr(source, "api_rate_limits", ()):
        if row.model not in names:
            continue
        for limit, remaining, reset in (
            (row.token_limit, row.tokens_remaining, row.tokens_reset_at),
            (row.request_limit, row.requests_remaining, row.requests_reset_at),
        ):
            if limit is not None and limit > 0 and remaining is not None and _current(row, reset, now):
                ratios.append(max(0, min(1, remaining / limit)))
    return min(ratios, default=1.0)


async def record_api_rate_headers(source: ModelSource, model: object, headers: Mapping[str, str]) -> None:
    if source.billing_mode != "api_metered" or source.auth_type != "api_key" or not isinstance(model, str):
        return
    if model not in {entry.upstream_model or entry.model for entry in source.models}:
        return
    observations = parse_api_rate_headers(source.provider, headers, model)
    if not observations:
        return
    try:
        async with get_background_session() as session:
            # Lock the parent before upserting; a credential/root cutover clears
            # this generation's telemetry and must not be overwritten by it.
            applied = await session.execute(update(ModelSource).where(
                ModelSource.id == source.id,
                ModelSource.provider == source.provider,
                ModelSource.auth_type == source.auth_type,
                ModelSource.billing_mode == source.billing_mode,
                ModelSource.api_key_encrypted == source.api_key_encrypted,
                ModelSource.base_url == source.base_url,
                ModelSource.messages_base_url == source.messages_base_url,
            ).values(updated_at=ModelSource.updated_at))
            if applied.rowcount != 1:
                return
            for observation in observations:
                observation.source_id = source.id
                previous = await session.get(ModelSourceApiRateLimit, (source.id, model, observation.token_kind))
                if previous is None or to_utc_naive(previous.observed_at) <= observation.observed_at:
                    await session.merge(observation)
            await session.commit()
    except SQLAlchemyError:
        # Telemetry storage is not inference failure or permission to retry an
        # already accepted request. Keep a safe, explicit operational warning.
        logger.warning("API rate-window telemetry could not be persisted", exc_info=False)


def api_usage_response(source: ModelSource) -> ModelSourceApiUsageResponse:
    limits = [ModelSourceApiRateLimitResponse(
        model=row.model, token_kind=row.token_kind, observed_at=row.observed_at,
        token_limit=row.token_limit, tokens_remaining=row.tokens_remaining, tokens_reset_at=row.tokens_reset_at,
        request_limit=row.request_limit, requests_remaining=row.requests_remaining,
        requests_reset_at=row.requests_reset_at,
    ) for row in sorted(source.api_rate_limits, key=lambda row: (row.model, row.token_kind))]
    balances = [
        ModelSourceApiBalanceResponse.model_validate(row) for row in json.loads(source.api_balance_json or "[]")
    ]
    times = [row.observed_at for row in source.api_rate_limits]
    if source.api_balance_observed_at is not None:
        times.append(source.api_balance_observed_at)
    return ModelSourceApiUsageResponse(
        source_id=source.id, observed_at=max(times, default=None), rate_limits=limits,
        balances=balances, balance_observed_at=source.api_balance_observed_at,
        balance_is_available=source.api_balance_is_available,
        balance_status=("available" if source.api_balance_observed_at else "unknown")
        if source.provider == "deepseek" else "unavailable",
        balance_error=source.quota_error if source.provider == "deepseek" else None,
    )
