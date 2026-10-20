"""Pure actual-token / provider-ratio inference, independent of chart decimation.

Nonoverlapping intervals accumulate at least five percentage points before
being admitted. Their ratio-weighted median is resistant to tiny rounded
samples and isolated outliers. The displayed range is the empirical interval
range, NOT a confidence interval, guaranteed bound, or official token limit.

RequestUsage timestamps explicitly describe execution, not insertion. Completed
calls are end-dated. Edge-spanning calls contribute zero-to-full token uncertainty;
accept only when that entire allowance is at most 10% of recorded interval usage.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.modules.quota_estimates.schemas import EstimateReason, QuotaEstimate

RETENTION = timedelta(days=30)
CURRENT_MAX_AGE = timedelta(minutes=5)
MIN_RATIO_DELTA = 0.05
MAX_BOUNDARY_FRACTION = 0.10
CAVEATS = (
    "Empirical gateway-observed token equivalent, not an official provider token cap or entitlement.",
    "Outside-gateway usage and missing retained logs can bias the estimate downward; no guaranteed bound.",
    "Model, cache, reasoning and provider weighting can change token equivalents; future mix may differ.",
    "Provider rounding, reporting delay and unlogged in-flight requests cannot be reconstructed.",
    "Uses normalized input plus output once; cache and reasoning subtotals are not added again.",
    "Range includes interval variation and boundary-token allowance, not a statistical confidence interval.",
    "Boundary-spanning usage is bounded zero-to-full, not time-prorated; allowance is at most 10% of recorded tokens.",
)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Observation:
    observed_at: datetime
    used_ratio: float | None
    resets_at: datetime | None


@dataclass(frozen=True, slots=True)
class RequestUsage:
    """One deduplicated provider call with already normalized actual usage.

    Missing output is NOT replaced by reasoning tokens: reasoning is only a
    subset and cannot establish complete output. A missing duration is unknown
    execution timing, not a zero-duration call. Non-success calls invalidate
    their intervals even when they have partial token usage.
    """

    started_at: datetime | None
    completed_at: datetime
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    success: bool = True
    cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None


def _valid_ratio(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 1


def _valid_count(value: int | None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _UsageIndex:
    """Start/end prefix sums bound edges without scanning requests per sample."""

    def __init__(self, requests: Sequence[RequestUsage]):
        ordered = sorted(requests, key=lambda request: utc(request.completed_at))
        self.ends: list[datetime] = []
        self.tokens = [0]
        self.missing = [0]
        self.unknown_ends: list[datetime] = []
        spans: list[tuple[datetime, int, int]] = []
        for request in ordered:
            end = utc(request.completed_at)
            start = utc(request.started_at) if request.started_at is not None else None
            if start is None or start > end:
                self.unknown_ends.append(end)
                continue
            valid = request.success and _valid_count(request.input_tokens) and _valid_count(request.output_tokens)
            tokens = request.input_tokens + request.output_tokens if valid else 0
            missing = int(not valid)
            spans.append((start, tokens, missing))
            self.ends.append(end)
            self.tokens.append(self.tokens[-1] + tokens)
            self.missing.append(self.missing[-1] + missing)
        spans.sort(key=lambda span: span[0])
        self.starts = [span[0] for span in spans]
        self.started_tokens = [0]
        self.started_missing = [0]
        for _, tokens, missing in spans:
            self.started_tokens.append(self.started_tokens[-1] + tokens)
            self.started_missing.append(self.started_missing[-1] + missing)

    def _boundary(self, at: datetime) -> tuple[int, int]:
        # Equality is ambiguous: include a call on either timestamp boundary.
        started = bisect_right(self.starts, at)
        ended = bisect_left(self.ends, at)
        return (
            self.started_tokens[started] - self.tokens[ended],
            self.started_missing[started] - self.missing[ended],
        )

    def interval(self, left: datetime, right: datetime) -> tuple[int, int, int, int, int, EstimateReason | None]:
        # Missing timing or token usage has no finite upper allowance. Keep
        # known usage for a separately labeled recorded minimum, never zero-fill.
        incomplete = bisect_left(self.unknown_ends, left) < len(self.unknown_ends)
        lo, hi = bisect_right(self.ends, left), bisect_left(self.ends, right)
        left_tokens, left_missing = self._boundary(left)
        right_tokens, right_missing = self._boundary(right)
        incomplete |= bool(self.missing[hi] != self.missing[lo] or left_missing or right_missing)
        tokens = self.tokens[hi] - self.tokens[lo]
        allowance = left_tokens + right_tokens
        low, high = max(0, tokens - left_tokens), tokens + right_tokens
        if incomplete:
            return tokens, hi - lo, low, high, allowance, "incomplete_usage"
        if allowance > tokens * MAX_BOUNDARY_FRACTION:
            return 0, 0, 0, 0, 0, "boundary_overlap"
        # A call spanning both edges is deliberately counted twice in the
        # allowance. Bounds remain conservative; no uniform token-rate guess.
        return tokens, hi - lo, low, high, allowance, None


def estimate_quota(
    *,
    window_key: str,
    model: str | None,
    observations: Sequence[Observation],
    requests: Sequence[RequestUsage],
    now: datetime,
    current: Observation | None = None,
    matching_models: frozenset[str] | None = None,
    unavailable_reason: EstimateReason | None = None,
) -> QuotaEstimate:
    """Return one DTO even when unknown/insufficient; never manufacture zero.

    ``matching_models=None`` means global coverage. For model-scoped quotas,
    callers MUST supply exact proven upstream IDs/aliases (no substring/family
    guesses). Current may be supplied separately from retained raw history.
    Only the current reset identity is used; no old-window extrapolation.
    """
    now = utc(now)
    result = QuotaEstimate(window_key=window_key, model=model, caveats=list(CAVEATS))
    history = [o for o in observations if now - RETENTION <= utc(o.observed_at) <= now]
    if current is None and observations:
        current = max(observations, key=lambda sample: utc(sample.observed_at))
    if current is not None:
        result.current_observed_at = utc(current.observed_at)
        result.current_used_ratio = current.used_ratio if _valid_ratio(current.used_ratio) else None
        result.resets_at = utc(current.resets_at) if current.resets_at is not None else None
    if unavailable_reason is not None:
        result.reason = unavailable_reason
        return result
    if model is not None and not matching_models:
        result.reason = "unsupported_scope"
        return result
    if current is None:
        return result
    if utc(current.observed_at) > now:
        result.reason = "current_future"
        return result
    if now - utc(current.observed_at) > CURRENT_MAX_AGE:
        result.reason = "current_stale"
        return result
    if current.resets_at is None:
        result.reason = "missing_reset"
        return result
    reset = utc(current.resets_at)
    if reset <= now:
        result.reason = "reset_elapsed"
        return result
    if not _valid_ratio(current.used_ratio):
        result.reason = "inconsistent_observations"
        return result
    history.append(current)
    by_time: dict[datetime, Observation] = {}
    for sample in history:
        at = utc(sample.observed_at)
        if sample.resets_at is None or utc(sample.resets_at) != reset or at > utc(current.observed_at):
            continue
        if not _valid_ratio(sample.used_ratio) or at >= reset:
            result.reason = "inconsistent_observations"
            return result
        previous = by_time.get(at)
        if previous is not None and previous.used_ratio != sample.used_ratio:
            result.reason = "inconsistent_observations"
            return result
        by_time[at] = sample
    samples = sorted(by_time.items())
    result.observation_count = len(samples)
    result.status = "insufficient"
    result.reason = "insufficient_delta"
    if len(samples) < 2:
        return result
    if any(right[1].used_ratio < left[1].used_ratio for left, right in zip(samples, samples[1:])):
        result.reason = "inconsistent_observations"
        return result
    covered = []
    for request in requests:
        if utc(request.completed_at) > now:
            continue
        if matching_models is None or request.model in matching_models:
            covered.append(request)
        elif request.model is None:
            # Unknown model ownership is not proof that this call is outside
            # the quota. Keep it as incomplete evidence, never silently omit.
            covered.append(RequestUsage(request.started_at, request.completed_at, None, None, None, False))
    usage = _UsageIndex(covered)
    accepted: list[tuple[float, float, int, int, datetime, datetime, int, int, int]] = []
    partial: list[tuple[float, float, int, int, datetime, datetime, int, int, int]] = []
    anchor_at, anchor = samples[0]
    rejection: EstimateReason | None = None
    pending_boundary = False
    for at, sample in samples[1:]:
        delta = sample.used_ratio - anchor.used_ratio
        if delta + 1e-12 < MIN_RATIO_DELTA:
            continue
        tokens, count, low, high, allowance, problem = usage.interval(anchor_at, at)
        if problem == "boundary_overlap":
            # A longer interval can dilute a bounded edge without pretending
            # that every in-flight request consumed all its tokens at one end.
            pending_boundary = True
            continue
        pending_boundary = False
        if problem is not None:
            rejection = problem
            if problem == "incomplete_usage" and low > 0:
                partial.append((low / delta, delta, tokens, count, anchor_at, at, low, high, allowance))
        elif tokens > 0:
            accepted.append((tokens / delta, delta, tokens, count, anchor_at, at, low, high, allowance))
        else:
            rejection = "no_matched_usage"
        # Never reuse evidence from an accepted OR rejected interval.
        anchor_at, anchor = at, sample
    lower_bound = not accepted and bool(partial)
    if lower_bound:
        accepted = partial
    if not accepted:
        result.reason = "boundary_overlap" if pending_boundary else rejection or "insufficient_delta"
        return result
    by_capacity = sorted(accepted)
    midpoint = sum(interval[1] for interval in by_capacity) / 2
    weight = 0.0
    capacity = by_capacity[-1][0]
    for interval in by_capacity:
        weight += interval[1]
        if weight + 1e-12 >= midpoint:
            capacity = interval[0]
            break
    result.status = "estimated"
    result.reason = "incomplete_usage" if lower_bound else "valid"
    result.estimate_kind = "recorded_lower_bound" if lower_bound else "empirical_estimate"
    result.estimated_total_tokens = capacity
    result.remaining_tokens = capacity * (1 - current.used_ratio)
    result.total_tokens_low = min(interval[6] / interval[1] for interval in accepted)
    result.total_tokens_high = None if lower_bound else max(interval[7] / interval[1] for interval in accepted)
    result.sample_count = len(accepted)
    result.observed_tokens = sum(interval[2] for interval in accepted)
    result.boundary_uncertainty_tokens = sum(interval[8] for interval in accepted)
    result.request_count = sum(interval[3] for interval in accepted)
    result.used_ratio_delta = sum(interval[1] for interval in accepted)
    result.evidence_start = accepted[0][4]
    result.evidence_end = accepted[-1][5]
    if lower_bound:
        result.caveats.append(
            "Recorded minimum equivalent only: unknown usage was not counted as zero; "
            "total allocation and upper range remain unknown."
        )
    if rejection:
        result.caveats.append("Some disjoint intervals were rejected for " + rejection + ".")
    return result
