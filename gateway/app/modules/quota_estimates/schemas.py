"""Read-only, empirical token equivalents; never provider entitlements or routing inputs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.modules.shared.schemas import DashboardModel

EstimateReason = Literal[
    "valid",
    "no_observations",
    "unsupported_scope",
    "identity_ambiguous",
    "missing_reset",
    "current_stale",
    "current_future",
    "reset_elapsed",
    "insufficient_delta",
    "incomplete_usage",
    "boundary_overlap",
    "inconsistent_observations",
    "no_matched_usage",
]


class QuotaEstimate(DashboardModel):
    window_key: str
    model: str | None = None
    status: Literal["estimated", "insufficient", "unknown"] = "unknown"
    reason: EstimateReason = "no_observations"
    estimated_total_tokens: float | None = None
    remaining_tokens: float | None = None
    total_tokens_low: float | None = None
    total_tokens_high: float | None = None
    observed_tokens: int = 0
    boundary_uncertainty_tokens: int = 0
    sample_count: int = 0
    observation_count: int = 0
    request_count: int = 0
    used_ratio_delta: float = 0.0
    evidence_start: datetime | None = None
    evidence_end: datetime | None = None
    current_observed_at: datetime | None = None
    current_used_ratio: float | None = None
    resets_at: datetime | None = None
    basis: Literal["observed_gateway_input_plus_output_tokens"] = "observed_gateway_input_plus_output_tokens"
    estimate_kind: Literal["empirical_estimate", "recorded_lower_bound"] | None = None
    caveats: list[str] = Field(default_factory=list)


class AccountQuotaEstimates(DashboardModel):
    account_id: str
    shared_account_ids: list[str] = Field(default_factory=list)
    windows: list[QuotaEstimate]


class SourceQuotaEstimates(DashboardModel):
    source_id: str
    shared_source_ids: list[str] = Field(default_factory=list)
    windows: list[QuotaEstimate]


class QuotaEstimatesResponse(DashboardModel):
    accounts: list[AccountQuotaEstimates] = Field(default_factory=list)
    sources: list[SourceQuotaEstimates] = Field(default_factory=list)
