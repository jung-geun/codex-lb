from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.modules.model_sources.schemas import ModelSourceQuotaWindowResponse
from app.modules.shared.schemas import DashboardModel


class LimitRuleCreate(DashboardModel):
    limit_type: str = Field(pattern=r"^(total_tokens|input_tokens|output_tokens|cost_usd|credits)$")
    limit_window: str = Field(pattern=r"^(daily|weekly|monthly|5h|7d)$")
    max_value: int = Field(ge=1)
    model_filter: str | None = None


class LimitRuleResponse(DashboardModel):
    id: int
    limit_type: str
    limit_window: str
    max_value: int
    current_value: int
    model_filter: str | None
    reset_at: datetime


class ApiKeyCreateRequest(DashboardModel):
    name: str = Field(min_length=1, max_length=128)
    billing_policy: Literal["any", "subscription_only"] = "any"
    allowed_models: list[str] | None = None
    apply_to_codex_model: bool = False
    enforced_model: str | None = Field(default=None, min_length=1)
    enforced_reasoning_effort: str | None = Field(
        default=None, pattern=r"(?i)^(none|minimal|low|medium|high|xhigh|max|ultra)$"
    )
    allowed_reasoning_efforts: list[str] | None = None
    enforced_service_tier: str | None = Field(default=None, pattern=r"(?i)^(auto|default|priority|flex|(ultra)?fast)$")
    traffic_class: str | None = Field(default=None, pattern=r"(?i)^(foreground|opportunistic)$")
    transport_policy_override: str | None = None
    thread_cache_identity_override: str | None = None
    usage_sections: str | None = None
    weekly_token_limit: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    assigned_account_ids: list[str] | None = None
    assigned_source_ids: list[str] | None = None
    limits: list[LimitRuleCreate] | None = None


class ApiKeyUpdateRequest(DashboardModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    billing_policy: Literal["any", "subscription_only"] | None = None
    allowed_models: list[str] | None = None
    apply_to_codex_model: bool | None = None
    enforced_model: str | None = Field(default=None, min_length=1)
    enforced_reasoning_effort: str | None = Field(
        default=None, pattern=r"(?i)^(none|minimal|low|medium|high|xhigh|max|ultra)$"
    )
    allowed_reasoning_efforts: list[str] | None = None
    enforced_service_tier: str | None = Field(default=None, pattern=r"(?i)^(auto|default|priority|flex|(ultra)?fast)$")
    traffic_class: str | None = Field(default=None, pattern=r"(?i)^(foreground|opportunistic)$")
    transport_policy_override: str | None = None
    thread_cache_identity_override: str | None = None
    usage_sections: str | None = None
    weekly_token_limit: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    is_active: bool | None = None
    assigned_account_ids: list[str] | None = None
    assigned_source_ids: list[str] | None = None
    limits: list[LimitRuleCreate] | None = None
    reset_usage: bool | None = None


class ApiKeyUsageSummaryResponse(DashboardModel):
    request_count: int
    total_tokens: int
    cached_input_tokens: int
    total_cost_usd: float


class ApiKeyResponse(DashboardModel):
    id: str
    name: str
    key_prefix: str
    billing_policy: Literal["any", "subscription_only"] = "any"
    allowed_models: list[str] | None
    apply_to_codex_model: bool = False
    enforced_model: str | None
    enforced_reasoning_effort: str | None
    allowed_reasoning_efforts: list[str] | None
    enforced_service_tier: str | None
    traffic_class: str
    transport_policy_override: str | None = None
    thread_cache_identity_override: str | None = None
    usage_sections: str = "upstream_limits,account_pool_usage"
    expires_at: datetime | None
    is_active: bool
    account_assignment_scope_enabled: bool = False
    source_assignment_scope_enabled: bool = False
    assigned_account_ids: list[str] = Field(default_factory=list)
    assigned_source_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    last_used_at: datetime | None
    limits: list[LimitRuleResponse] = Field(default_factory=list)
    usage_summary: ApiKeyUsageSummaryResponse | None = None
    pooled_remaining_percent_primary: float | None = None
    pooled_remaining_percent_secondary: float | None = None
    pooled_capacity_credits_primary: float = 0.0


class ApiKeyCreateResponse(ApiKeyResponse):
    key: str


class ApiKeyTrendPoint(DashboardModel):
    t: datetime
    v: float


class ApiKeyTrendsResponse(DashboardModel):
    key_id: str
    cost: list[ApiKeyTrendPoint] = Field(default_factory=list)
    tokens: list[ApiKeyTrendPoint] = Field(default_factory=list)


class ApiKeyAccountCostResponse(DashboardModel):
    account_id: str | None = None
    email: str | None = None
    cost_usd: float = 0
    is_deleted: bool = False
    model_source_id: str | None = None
    source_name: str | None = None
    provider: str | None = None
    billing_mode: str | None = None
    request_count: int = 0
    unpriced_request_count: int = 0


class ApiKeyUsage7DayResponse(DashboardModel):
    key_id: str
    total_tokens: int = 0
    total_cost_usd: float = 0
    total_requests: int = 0
    cached_input_tokens: int = 0
    account_costs: list[ApiKeyAccountCostResponse] = Field(default_factory=list)


class SourceModelAccessResponse(DashboardModel):
    source_id: str
    source_name: str
    provider: str
    billing_mode: str
    model: str
    display_name: str
    availability: Literal["available", "quota_exhausted", "unavailable", "restricted"]
    quota_windows: list[ModelSourceQuotaWindowResponse] = Field(default_factory=list)


class SourceAccessResponse(DashboardModel):
    models: list[SourceModelAccessResponse] = Field(default_factory=list)
