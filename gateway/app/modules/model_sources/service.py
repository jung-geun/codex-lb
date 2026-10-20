from __future__ import annotations

import json
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from app.core.crypto import TokenEncryptor
from app.core.utils.time import to_utc_naive, utcnow
from app.db.models import ModelSource, ModelSourceModel
from app.modules.model_sources.provider_schemas import presets
from app.modules.model_sources.repository import ModelSourceHasActiveOperationsError, ModelSourcesRepository
from app.modules.model_sources.schemas import (
    ModelSourceCreateRequest,
    ModelSourceLimitWarmupStatus,
    ModelSourceModelInput,
    ModelSourceModelResponse,
    ModelSourceQuotaHistoryResponse,
    ModelSourceQuotaWindowResponse,
    ModelSourceResponse,
    ModelSourceUpdateRequest,
    ModelSourceUsageResponse,
)

MODEL_SOURCE_KIND_OPENAI_COMPATIBLE = "openai_compatible"
MODEL_SOURCE_HEALTH_UNKNOWN = "unknown"


class ModelSourceNotFoundError(ValueError):
    pass


class ModelSourceValidationError(ValueError):
    pass


class ModelSourceInUseError(ValueError):
    pass


class ModelSourcesService:
    def __init__(
        self,
        repository: ModelSourcesRepository,
        *,
        encryptor: TokenEncryptor | None = None,
    ) -> None:
        self._repository = repository
        self._encryptor = encryptor or TokenEncryptor()

    async def list_sources(self) -> list[ModelSourceResponse]:
        rows = await self._repository.list_sources()
        return [_to_response(row) for row in rows]

    async def list_enabled_sources(self) -> list[ModelSource]:
        return await self._repository.list_enabled_sources()

    async def source_usage(self, source_id: str, *, days: int = 1) -> ModelSourceUsageResponse:
        if await self._repository.get_by_id(source_id) is None:
            raise ModelSourceNotFoundError(f"Model source not found: {source_id}")
        now = utcnow()
        return await self._repository.usage_summary(
            source_id, window_start=now - timedelta(days=days), window_end=now
        )

    async def source_quota_history(self, source_id: str, *, days: int = 7) -> ModelSourceQuotaHistoryResponse:
        if await self._repository.get_by_id(source_id) is None:
            raise ModelSourceNotFoundError(f"Model source not found: {source_id}")
        now = utcnow()
        start = now - timedelta(days=days)
        rows = await self._repository.quota_history(source_id, window_start=start, window_end=now)
        return ModelSourceQuotaHistoryResponse(
            source_id=source_id,
            window_start=start,
            window_end=now,
            windows=[
                ModelSourceQuotaWindowResponse(
                    window_key=row.window_key, model=row.model, used_ratio=row.used_ratio,
                    limit_value=row.limit_value, used_value=row.used_value,
                    resets_at=row.resets_at, observed_at=row.observed_at,
                )
                for row in rows
            ],
        )

    async def create_source(self, payload: ModelSourceCreateRequest) -> ModelSourceResponse:
        model_rows = _model_inputs_to_rows(payload.models, provider=payload.provider)
        row = ModelSource(
            id=f"src_{uuid.uuid4().hex}",
            name=_normalize_name(payload.name),
            kind=MODEL_SOURCE_KIND_OPENAI_COMPATIBLE,
            base_url=_normalize_base_url(payload.base_url),
            api_key_encrypted=_encrypt_optional(self._encryptor, payload.api_key),
            provider=payload.provider,
            auth_type=payload.auth_type or ("api_key" if payload.api_key else "none"),
            billing_mode=payload.billing_mode,
            key_type=payload.key_type,
            detected_key_type="unknown",
            entitlement_status="unverified",
            supports_messages=payload.supports_messages,
            supports_count_tokens=payload.supports_count_tokens,
            messages_base_url=_normalize_base_url(payload.messages_base_url) if payload.messages_base_url else None,
            priority=payload.priority,
            api_fallback_only=payload.api_fallback_only,
            quota_group=payload.quota_group,
            routing_policy="normal",
            limit_warmup_enabled=False,
            limit_warmups=[],
            oauth_credential=None,
            quota_windows=[],
            is_enabled=True,
            health_status=MODEL_SOURCE_HEALTH_UNKNOWN,
            supports_chat_completions=payload.supports_chat_completions,
            supports_responses=payload.supports_responses,
            supports_audio_transcriptions=payload.supports_audio_transcriptions,
            supports_embeddings=payload.supports_embeddings,
            timeout_seconds=payload.timeout_seconds,
            max_concurrency=payload.max_concurrency,
            models=model_rows,
        )
        _apply_provider_preset(row, payload.model_fields_set, initialize=True)
        try:
            created = await self._repository.create(row, commit=True)
        except Exception:
            await self._repository.rollback()
            raise
        return _to_response(created)

    async def update_source(self, source_id: str, payload: ModelSourceUpdateRequest) -> ModelSourceResponse:
        row = await self._repository.get_by_id(source_id)
        if row is None:
            raise ModelSourceNotFoundError(f"Model source not found: {source_id}")

        fields = payload.model_fields_set
        if "routing_policy" in fields and payload.routing_policy is None:
            raise ModelSourceValidationError("routing_policy cannot be null")
        if "limit_warmup_enabled" in fields and payload.limit_warmup_enabled is None:
            raise ModelSourceValidationError("limit_warmup_enabled cannot be null")
        if "api_fallback_only" in fields and payload.api_fallback_only is None:
            raise ModelSourceValidationError("api_fallback_only cannot be null")
        history_identity_fields = {
            "provider", "auth_type", "base_url", "messages_base_url", "billing_mode", "api_key", "quota_group",
        }
        previous_history_ids = (
            await self._repository.quota_history_component_ids(source_id)
            if fields & history_identity_fields else set()
        )
        previous_identity = (
            row.provider, row.auth_type, row.base_url, row.messages_base_url or row.base_url, row.billing_mode,
        )
        group_changed = "quota_group" in fields and payload.quota_group != row.quota_group
        key_type_changed = "key_type" in fields and payload.key_type is not None and payload.key_type != row.key_type
        if "key_type" in fields and payload.key_type is not None:
            row.key_type = payload.key_type
        if "provider" in fields and payload.provider is not None:
            row.provider = payload.provider
        if "auth_type" in fields and payload.auth_type is not None:
            row.auth_type = payload.auth_type
        elif "api_key" in fields:
            row.auth_type = "api_key" if payload.api_key else "none"
        if "billing_mode" in fields and payload.billing_mode is not None:
            row.billing_mode = payload.billing_mode
        if "messages_base_url" in fields:
            row.messages_base_url = (
                _normalize_base_url(payload.messages_base_url) if payload.messages_base_url else None
            )
        if "supports_messages" in fields and payload.supports_messages is not None:
            row.supports_messages = payload.supports_messages
        if "supports_count_tokens" in fields and payload.supports_count_tokens is not None:
            row.supports_count_tokens = payload.supports_count_tokens
        if "priority" in fields and payload.priority is not None:
            row.priority = payload.priority
        if "routing_policy" in fields and payload.routing_policy is not None:
            row.routing_policy = payload.routing_policy
        if "api_fallback_only" in fields and payload.api_fallback_only is not None:
            row.api_fallback_only = payload.api_fallback_only
        if "limit_warmup_enabled" in fields and payload.limit_warmup_enabled is not None:
            row.limit_warmup_enabled = payload.limit_warmup_enabled
        if "name" in fields and payload.name is not None:
            row.name = _normalize_name(payload.name)
        if "base_url" in fields and payload.base_url is not None:
            row.base_url = _normalize_base_url(payload.base_url)
        if "api_key" in fields:
            row.api_key_encrypted = _encrypt_optional(self._encryptor, payload.api_key)
        if "is_enabled" in fields and payload.is_enabled is not None:
            row.is_enabled = payload.is_enabled
        if "supports_chat_completions" in fields and payload.supports_chat_completions is not None:
            row.supports_chat_completions = payload.supports_chat_completions
        if "supports_responses" in fields and payload.supports_responses is not None:
            row.supports_responses = payload.supports_responses
        if "supports_audio_transcriptions" in fields and payload.supports_audio_transcriptions is not None:
            row.supports_audio_transcriptions = payload.supports_audio_transcriptions
        if "supports_embeddings" in fields and payload.supports_embeddings is not None:
            row.supports_embeddings = payload.supports_embeddings
        if "timeout_seconds" in fields:
            row.timeout_seconds = payload.timeout_seconds
        if "max_concurrency" in fields:
            row.max_concurrency = payload.max_concurrency
        _apply_provider_preset(row, fields, provider_changed=row.provider != previous_identity[0])
        if row.limit_warmup_enabled:
            from app.modules.model_sources.subscription_warmup import warmup_supported

            if not warmup_supported(row):
                raise ModelSourceValidationError("Limit warmup is supported only for fixed Claude OAuth subscriptions")
        identity_changed = previous_identity != (
            row.provider,
            row.auth_type,
            row.base_url,
            row.messages_base_url or row.base_url,
            row.billing_mode,
        )
        coverage_changed = bool(
            fields
            & {
                "models",
                "supports_messages",
                "supports_count_tokens",
                "supports_chat_completions",
                "supports_responses",
                "supports_audio_transcriptions",
                "supports_embeddings",
            }
        )
        if identity_changed or "api_key" in fields or coverage_changed or group_changed or key_type_changed:
            _invalidate_entitlement(row)
        if identity_changed or "api_key" in fields:
            row.api_rate_limits.clear()
            row.api_balance_json = None
            row.api_balance_observed_at = None
            row.api_balance_is_available = None
            row.oauth_credential = None
            row.quota_group = None
        if "quota_group" in fields:
            row.quota_group = payload.quota_group

        models_replaced = False
        try:
            if identity_changed or "api_key" in fields or group_changed:
                current_history_ids = await self._repository.quota_history_component_ids(source_id)
                await self._repository.clear_quota_history(previous_history_ids | current_history_ids)
            if "models" in fields and payload.models is not None:
                await self._repository.replace_models(
                    row, _model_inputs_to_rows(payload.models, provider=row.provider), commit=False
                )
                models_replaced = True
            await self._repository.commit()
        except Exception:
            await self._repository.rollback()
            raise

        if models_replaced:
            # ``replace_models`` bulk-deletes and re-inserts child rows without
            # touching the identity-mapped parent's already-loaded ``models``
            # collection, so the post-commit read would return the stale
            # pre-update list without an explicit refresh.
            await self._repository.refresh_models(row)
        refreshed = await self._repository.get_by_id(source_id)
        if refreshed is None:
            raise ModelSourceNotFoundError(f"Model source not found: {source_id}")
        return _to_response(refreshed)

    async def delete_source(self, source_id: str) -> None:
        try:
            deleted = await self._repository.delete(source_id)
        except ModelSourceHasActiveOperationsError as exc:
            raise ModelSourceInUseError(
                "Source has active or uncertain requests; resolve them before deletion"
            ) from exc
        if not deleted:
            raise ModelSourceNotFoundError(f"Model source not found: {source_id}")


def _invalidate_entitlement(source: ModelSource) -> None:
    source.entitlement_status = "unverified"
    source.entitlement_verified_at = None
    source.entitlement_detail = None
    source.extra_usage_enabled = None
    source.quota_checked_at = None
    source.quota_error = None
    source.detected_key_type = "unknown"
    source.quota_windows.clear()
    source.cooldown_until = None
    source.health_status = "unknown"
    for model in source.models:
        model.subscription_eligible = False


def _apply_provider_preset(
    source: ModelSource, fields: set[str], *, initialize: bool = False, provider_changed: bool = False,
) -> None:
    if source.provider == "custom":
        if source.auth_type == "claude_oauth":
            raise ModelSourceValidationError("Claude OAuth requires the Claude provider preset")
        source.kind = (
            "anthropic_compatible"
            if source.supports_messages and not source.supports_chat_completions
            else MODEL_SOURCE_KIND_OPENAI_COMPATIBLE
        )
        return
    preset = next((item for item in presets() if item.provider == source.provider), None)
    if preset is None:
        raise ModelSourceValidationError("Unknown model source provider")
    if source.base_url != preset.base_url:
        raise ModelSourceValidationError("Provider base_url must match its fixed preset endpoint")
    if "auth_type" in fields and source.auth_type != preset.auth_type:
        raise ModelSourceValidationError("Provider auth_type must match its preset")
    if "billing_mode" in fields and source.billing_mode != preset.billing_mode:
        raise ModelSourceValidationError("Provider billing_mode must match its preset")
    if "messages_base_url" in fields and source.messages_base_url != preset.messages_base_url:
        raise ModelSourceValidationError("Provider messages_base_url must match its fixed preset endpoint")
    for name in (
        "supports_messages", "supports_chat_completions", "supports_count_tokens",
        "supports_responses", "supports_embeddings", "supports_audio_transcriptions",
    ):
        if initialize and name not in fields:
            setattr(source, name, getattr(preset, name))
        elif getattr(source, name) and not getattr(preset, name):
            raise ModelSourceValidationError("Provider does not support the requested protocol capability")
    if source.provider in {"openai", "anthropic"}:
        if not source.api_key_encrypted:
            raise ModelSourceValidationError("Connect a provider API key for this metered preset")
        for model in source.models:
            model.model, model.upstream_model = _metered_model_identity(
                source.provider, model.model, model.upstream_model
            )
    # Native inspection can refine billing beyond the preset's initial label.
    # Metadata/routing edits must not silently replace that verified identity.
    source.auth_type = preset.auth_type
    source.messages_base_url = preset.messages_base_url
    if initialize or provider_changed:
        source.billing_mode = preset.billing_mode
    source.kind = (
        "anthropic_compatible"
        if source.supports_messages and not source.supports_chat_completions
        else MODEL_SOURCE_KIND_OPENAI_COMPATIBLE
    )


def _normalize_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ModelSourceValidationError("Model source name is required")
    return name


def _normalize_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelSourceValidationError("Model source base_url must be an absolute HTTP(S) URL")
    return url


def _normalize_model_slug(value: str) -> str:
    model = value.strip()
    if not model:
        raise ModelSourceValidationError("Model source model name is required")
    return model


def _validate_raw_metadata_json(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelSourceValidationError("raw_metadata_json must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelSourceValidationError("raw_metadata_json must be a JSON object")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _metered_model_identity(provider: str, model: str, upstream_model: str | None) -> tuple[str, str]:
    prefix = provider + "/"
    native = model.removeprefix(prefix)
    alias = prefix + native
    if not native or len(alias) > 255:
        raise ModelSourceValidationError("Metered model alias must contain an ID and fit within 255 characters")
    return alias, upstream_model or native


def _model_inputs_to_rows(
    models: list[ModelSourceModelInput], *, provider: str = "custom"
) -> list[ModelSourceModel]:
    seen: set[str] = set()
    rows: list[ModelSourceModel] = []
    for item in models:
        model = _normalize_model_slug(item.model)
        upstream_model = _normalize_model_slug(item.upstream_model) if item.upstream_model else None
        if provider in {"openai", "anthropic"}:
            model, upstream_model = _metered_model_identity(provider, model, upstream_model)
        if item.subscription_eligible:
            raise ModelSourceValidationError("Only provider validation can enable subscription model coverage")
        if model in seen:
            raise ModelSourceValidationError(f"Duplicate model source model: {model}")
        seen.add(model)
        rows.append(
            ModelSourceModel(
                model=model,
                upstream_model=upstream_model,
                subscription_eligible=False,
                subscription_restricted="subscription_eligible" in item.model_fields_set
                and not item.subscription_eligible,
                cache_creation_per_1m=item.cache_creation_per_1m,
                cache_creation_1h_per_1m=item.cache_creation_1h_per_1m,
                display_name=item.display_name.strip() if item.display_name else None,
                context_window=item.context_window,
                max_output_tokens=item.max_output_tokens,
                supports_streaming=item.supports_streaming,
                supports_tools=item.supports_tools,
                supports_vision=item.supports_vision,
                input_per_1m=item.input_per_1m,
                cached_input_per_1m=item.cached_input_per_1m,
                output_per_1m=item.output_per_1m,
                audio_per_minute=item.audio_per_minute,
                raw_metadata_json=_validate_raw_metadata_json(item.raw_metadata_json),
                is_enabled=item.is_enabled,
            )
        )
    return rows


def _encrypt_optional(encryptor: TokenEncryptor, value: str | None) -> bytes | None:
    if value is None:
        return None
    secret = value.strip()
    if not secret:
        return None
    return encryptor.encrypt(secret)


def _to_model_response(row: ModelSourceModel) -> ModelSourceModelResponse:
    return ModelSourceModelResponse(
        id=row.id,
        source_id=row.source_id,
        model=row.model,
        upstream_model=row.upstream_model,
        subscription_eligible=bool(row.subscription_eligible) and not row.subscription_restricted,
        subscription_restricted=bool(row.subscription_restricted),
        cache_creation_per_1m=row.cache_creation_per_1m,
        cache_creation_1h_per_1m=row.cache_creation_1h_per_1m,
        display_name=row.display_name,
        context_window=row.context_window,
        max_output_tokens=row.max_output_tokens,
        supports_streaming=row.supports_streaming,
        supports_tools=row.supports_tools,
        supports_vision=row.supports_vision,
        input_per_1m=row.input_per_1m,
        cached_input_per_1m=row.cached_input_per_1m,
        output_per_1m=row.output_per_1m,
        audio_per_minute=row.audio_per_minute,
        raw_metadata_json=row.raw_metadata_json,
        is_enabled=row.is_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_response(row: ModelSource) -> ModelSourceResponse:
    from app.modules.model_sources.native_operations import NATIVE_LEASE_SECONDS
    from app.modules.model_sources.subscription_warmup import warmup_supported, warmup_unavailable_reason

    latest = row.limit_warmups[0] if row.limit_warmups else None
    expired_dispatch = (
        latest is not None
        and latest.status == "dispatching"
        and to_utc_naive(utcnow()) - to_utc_naive(latest.attempted_at) > timedelta(seconds=NATIVE_LEASE_SECONDS)
    )
    return ModelSourceResponse(
        id=row.id,
        name=row.name,
        kind=row.kind,
        base_url=row.base_url,
        provider=row.provider,
        auth_type=row.auth_type,
        billing_mode=row.billing_mode,
        key_type=row.key_type,
        detected_key_type=row.detected_key_type,
        entitlement_status=row.entitlement_status,
        entitlement_verified_at=row.entitlement_verified_at,
        entitlement_detail=row.entitlement_detail,
        extra_usage_enabled=row.extra_usage_enabled,
        supports_messages=row.supports_messages,
        supports_count_tokens=row.supports_count_tokens,
        messages_base_url=row.messages_base_url,
        priority=row.priority,
        routing_policy=row.routing_policy,
        api_fallback_only=row.api_fallback_only,
        limit_warmup_enabled=row.limit_warmup_enabled,
        limit_warmup=(
            ModelSourceLimitWarmupStatus(
                window=latest.window_key,
                reset_at=latest.reset_at,
                status="unknown" if expired_dispatch else latest.status,
                model=latest.model,
                attempted_at=latest.attempted_at,
                completed_at=latest.completed_at,
                error_message="acceptance_unknown" if expired_dispatch else latest.error_message,
            )
            if latest is not None else None
        ),
        warmup_supported=warmup_supported(row),
        warmup_unavailable_reason=warmup_unavailable_reason(row),
        quota_group=row.quota_group,
        cooldown_until=row.cooldown_until,
        quota_checked_at=row.quota_checked_at,
        quota_error=row.quota_error,
        quota_windows=[
            ModelSourceQuotaWindowResponse(
                window_key=window.window_key,
                model=window.model,
                used_ratio=window.used_ratio,
                limit_value=window.limit_value,
                used_value=window.used_value,
                resets_at=window.resets_at,
                observed_at=window.observed_at,
            )
            for window in row.quota_windows
        ],
        credential_expires_at=row.oauth_credential.expires_at if row.oauth_credential else None,
        account_label=row.oauth_credential.account_label if row.oauth_credential else None,
        subscription_type=row.oauth_credential.subscription_type if row.oauth_credential else None,
        account_uuid=row.oauth_credential.account_uuid if row.oauth_credential else None,
        organization_uuid=row.oauth_credential.organization_uuid if row.oauth_credential else None,
        has_access_token=bool(
            row.api_key_encrypted or (row.oauth_credential and row.oauth_credential.access_token_encrypted)
        ),
        has_refresh_token=bool(row.oauth_credential and row.oauth_credential.refresh_token_encrypted),
        is_enabled=row.is_enabled,
        health_status=row.health_status,
        supports_chat_completions=row.supports_chat_completions,
        supports_responses=row.supports_responses,
        supports_audio_transcriptions=row.supports_audio_transcriptions,
        supports_embeddings=row.supports_embeddings,
        timeout_seconds=row.timeout_seconds,
        max_concurrency=row.max_concurrency,
        created_at=row.created_at,
        updated_at=row.updated_at,
        models=[_to_model_response(model) for model in row.models],
    )
