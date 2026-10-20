from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from app.modules.shared.schemas import DashboardModel

SourceKeyType = Literal["personal", "team", "compatible", "standard", "unknown"]
SourceRoutingPolicy = Literal["normal", "burn_first", "preserve"]


class ModelSourceModelInput(DashboardModel):
    model: str = Field(min_length=1, max_length=255)
    upstream_model: str | None = Field(default=None, min_length=1, max_length=255)
    subscription_eligible: bool = False
    cache_creation_per_1m: float | None = Field(default=None, ge=0)
    cache_creation_1h_per_1m: float | None = Field(default=None, ge=0)
    display_name: str | None = Field(default=None, max_length=255)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_vision: bool = False
    input_per_1m: float | None = Field(default=None, ge=0)
    cached_input_per_1m: float | None = Field(default=None, ge=0)
    output_per_1m: float | None = Field(default=None, ge=0)
    audio_per_minute: float | None = Field(default=None, ge=0)
    raw_metadata_json: str | None = None
    is_enabled: bool = True


class ModelSourceModelResponse(ModelSourceModelInput):
    id: int
    source_id: str
    subscription_restricted: bool = False
    created_at: datetime
    updated_at: datetime


class ModelSourceCreateRequest(DashboardModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=2048)
    provider: Literal["custom", "claude", "zai", "kimi", "nanogpt", "deepseek", "openai", "anthropic"] = "custom"
    auth_type: Literal["none", "api_key", "claude_oauth"] | None = None
    billing_mode: Literal["subscription", "coding_plan", "api_metered", "unknown"] = "unknown"
    key_type: SourceKeyType = "unknown"
    supports_messages: bool = False
    supports_count_tokens: bool = False
    messages_base_url: str | None = Field(default=None, max_length=2048)
    priority: int = 0
    api_fallback_only: bool = True
    quota_group: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]*$"
    )
    api_key: str | None = Field(default=None, min_length=1)
    supports_chat_completions: bool = True
    supports_responses: bool = False
    supports_audio_transcriptions: bool = False
    supports_embeddings: bool = False
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    models: list[ModelSourceModelInput] = Field(default_factory=list)


class ModelSourceUpdateRequest(DashboardModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    provider: Literal["custom", "claude", "zai", "kimi", "nanogpt", "deepseek", "openai", "anthropic"] | None = None
    auth_type: Literal["none", "api_key", "claude_oauth"] | None = None
    billing_mode: Literal["subscription", "coding_plan", "api_metered", "unknown"] | None = None
    key_type: SourceKeyType | None = None
    supports_messages: bool | None = None
    supports_count_tokens: bool | None = None
    messages_base_url: str | None = Field(default=None, max_length=2048)
    priority: int | None = None
    routing_policy: SourceRoutingPolicy | None = None
    api_fallback_only: bool | None = None
    limit_warmup_enabled: bool | None = None
    quota_group: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]*$"
    )
    api_key: str | None = Field(default=None, min_length=1)
    is_enabled: bool | None = None
    supports_chat_completions: bool | None = None
    supports_responses: bool | None = None
    supports_audio_transcriptions: bool | None = None
    supports_embeddings: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    models: list[ModelSourceModelInput] | None = None


class ModelSourceQuotaWindowResponse(DashboardModel):
    window_key: str
    model: str | None
    used_ratio: float | None
    limit_value: float | None
    used_value: float | None
    resets_at: datetime | None
    observed_at: datetime


class ModelSourceQuotaHistoryResponse(DashboardModel):
    source_id: str
    window_start: datetime
    window_end: datetime
    windows: list[ModelSourceQuotaWindowResponse] = Field(default_factory=list)


class ModelSourceLimitWarmupStatus(DashboardModel):
    window: str
    reset_at: datetime | None = None
    status: str
    model: str | None = None
    attempted_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class ModelSourceResponse(DashboardModel):
    id: str
    name: str
    kind: str
    base_url: str
    provider: str = "custom"
    auth_type: str = "none"
    billing_mode: str = "unknown"
    key_type: SourceKeyType = "unknown"
    detected_key_type: SourceKeyType = "unknown"
    entitlement_status: str = "unverified"
    entitlement_verified_at: datetime | None = None
    entitlement_detail: str | None = None
    extra_usage_enabled: bool | None = None
    supports_messages: bool = False
    supports_count_tokens: bool = False
    messages_base_url: str | None = None
    priority: int = 0
    routing_policy: SourceRoutingPolicy = "normal"
    api_fallback_only: bool = True
    limit_warmup_enabled: bool = False
    limit_warmup: ModelSourceLimitWarmupStatus | None = None
    warmup_supported: bool = False
    warmup_unavailable_reason: str | None = None
    quota_group: str | None = None
    cooldown_until: datetime | None = None
    quota_checked_at: datetime | None = None
    quota_error: str | None = None
    quota_windows: list[ModelSourceQuotaWindowResponse] = Field(default_factory=list)
    credential_expires_at: datetime | None = None
    account_label: str | None = None
    subscription_type: str | None = None
    account_uuid: str | None = None
    organization_uuid: str | None = None
    has_access_token: bool = False
    has_refresh_token: bool = False
    is_enabled: bool
    health_status: str
    supports_chat_completions: bool
    supports_responses: bool
    supports_audio_transcriptions: bool
    supports_embeddings: bool
    timeout_seconds: int | None
    max_concurrency: int | None
    created_at: datetime
    updated_at: datetime
    models: list[ModelSourceModelResponse] = Field(default_factory=list)


class ModelSourcesResponse(DashboardModel):
    sources: list[ModelSourceResponse] = Field(default_factory=list)


class ModelSourceUsageResponse(DashboardModel):
    """Retained local request logs only; never an upstream bill or quota."""

    source_id: str
    window_start: datetime
    window_end: datetime
    request_count: int
    success_count: int
    error_count: int
    cancelled_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_creation_input_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    known_cost_usd: float | None
    unpriced_request_count: int
    unknown_usage_count: int
    last_request_at: datetime | None


class ModelSourceApiRateLimitResponse(DashboardModel):
    model: str | None
    token_kind: Literal["tokens", "input_tokens", "output_tokens", "project_tokens"] = "tokens"
    observed_at: datetime
    token_limit: int | None = None
    tokens_remaining: int | None = None
    tokens_reset_at: datetime | None = None
    request_limit: int | None = None
    requests_remaining: int | None = None
    requests_reset_at: datetime | None = None


class ModelSourceApiBalanceResponse(DashboardModel):
    currency: str
    total_balance: str
    granted_balance: str | None = None
    topped_up_balance: str | None = None


class ModelSourceApiUsageResponse(DashboardModel):
    source_id: str
    observed_at: datetime | None = None
    rate_limits: list[ModelSourceApiRateLimitResponse] = Field(default_factory=list)
    balances: list[ModelSourceApiBalanceResponse] = Field(default_factory=list)
    balance_observed_at: datetime | None = None
    balance_status: Literal["available", "unavailable", "unknown"] = "unknown"
    balance_is_available: bool | None = None
    balance_error: str | None = None
