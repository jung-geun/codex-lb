from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.db.models import ModelSource
from app.modules.shared.schemas import DashboardModel


def configuration_fingerprint(source: ModelSource) -> str:
    """Bind authorization to management configuration, not polling timestamps."""
    fields = (
        source.id,
        source.provider,
        source.auth_type,
        source.base_url,
        source.messages_base_url,
        source.billing_mode,
        source.key_type,
        source.is_enabled,
        source.supports_messages,
        source.supports_count_tokens,
        source.supports_chat_completions,
        source.supports_responses,
        source.supports_audio_transcriptions,
        source.supports_embeddings,
        source.api_key_encrypted,
    )
    return hashlib.sha256(repr(fields).encode()).hexdigest()


class OAuthStartResponse(DashboardModel):
    flow_id: str
    authorization_url: str
    expires_at: datetime


class OAuthStatusResponse(DashboardModel):
    flow_id: str
    status: Literal["pending", "exchanging", "completed", "cancelled", "expired", "failed"]
    expires_at: datetime
    error: str | None = None


class OAuthCompleteRequest(DashboardModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    flow_id: str = Field(min_length=1, max_length=128)
    code: SecretStr = Field(min_length=1, max_length=8192)


class ImportedClaudeCredential(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    accessToken: SecretStr = Field(min_length=1, max_length=16384)
    refreshToken: SecretStr | None = Field(default=None, max_length=16384)
    expiresAt: float = Field(gt=0)


class CredentialEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    claudeAiOauth: ImportedClaudeCredential


class CredentialImportRequest(DashboardModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    credentials: CredentialEnvelope | ImportedClaudeCredential
    single_writer_confirmed: Literal[True]

    @field_validator("single_writer_confirmed", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("Explicit single-writer confirmation is required")
        return True


class ProviderPreset(DashboardModel):
    provider: str
    name: str
    base_url: str
    messages_base_url: str | None = None
    auth_type: str
    billing_mode: str
    supports_messages: bool = False
    supports_chat_completions: bool = False
    supports_count_tokens: bool = False
    supports_responses: bool = False
    supports_embeddings: bool = False
    supports_audio_transcriptions: bool = False
    models: list[str] = Field(default_factory=list)
    connection_note: str


def presets() -> list[ProviderPreset]:
    # Models/limits are intentionally empty: account-native discovery owns them.
    return [
        ProviderPreset(
            provider="openai",
            name="OpenAI API (metered)",
            base_url="https://api.openai.com/v1",
            auth_type="api_key",
            billing_mode="api_metered",
            supports_chat_completions=True,
            supports_responses=True,
            supports_embeddings=True,
            supports_audio_transcriptions=True,
            connection_note=(
                "API key verification lists available models only. API usage is metered, separate from "
                "ChatGPT/Codex subscriptions; no balance, quota, prices or inference access are verified."
            ),
        ),
        ProviderPreset(
            provider="anthropic",
            name="Anthropic API (metered)",
            base_url="https://api.anthropic.com",
            messages_base_url="https://api.anthropic.com",
            auth_type="api_key",
            billing_mode="api_metered",
            supports_messages=True,
            supports_count_tokens=True,
            connection_note=(
                "API key verification lists available models only. API usage is metered, separate from "
                "Claude subscriptions; no balance, quota, prices or inference access are verified."
            ),
        ),
        ProviderPreset(
            provider="claude",
            name="Claude Pro / Max / Team",
            base_url="https://api.anthropic.com",
            messages_base_url="https://api.anthropic.com",
            auth_type="claude_oauth",
            billing_mode="subscription",
            supports_messages=True,
            supports_count_tokens=True,
            connection_note=(
                "Sign in or import Claude Code credentials with one credential writer. Disable extra usage."
            ),
        ),
        ProviderPreset(
            provider="zai",
            name="Z.ai GLM Coding Plan",
            base_url="https://api.z.ai/api/coding/paas/v4",
            messages_base_url="https://api.z.ai/api/anthropic",
            auth_type="api_key",
            billing_mode="coding_plan",
            supports_messages=True,
            supports_chat_completions=True,
            connection_note=(
                "Requires provider-verified Coding/Compatible key classification and overage restriction; "
                "Standard keys spend balance."
            ),
        ),
        ProviderPreset(
            provider="kimi",
            name="Kimi Coding",
            base_url="https://api.kimi.com/coding/v1",
            messages_base_url="https://api.kimi.com/coding",
            auth_type="api_key",
            billing_mode="coding_plan",
            supports_messages=True,
            supports_chat_completions=True,
            connection_note=(
                "Use a Kimi Coding credential. Unknown plan/model coverage remains unavailable "
                "for subscription-only routing."
            ),
        ),
        ProviderPreset(
            provider="nanogpt",
            name="NanoGPT subscription",
            base_url="https://nano-gpt.com/api/v1",
            auth_type="api_key",
            billing_mode="subscription",
            supports_chat_completions=True,
            connection_note="Requires active subscription, covered models, and pay-as-you-go fallback disabled.",
        ),
        ProviderPreset(
            provider="deepseek",
            name="DeepSeek direct (metered, not subscription)",
            base_url="https://api.deepseek.com",
            auth_type="api_key",
            billing_mode="api_metered",
            supports_chat_completions=True,
            connection_note="Direct DeepSeek spends API balance and is excluded from subscription-only routing.",
        ),
    ]
