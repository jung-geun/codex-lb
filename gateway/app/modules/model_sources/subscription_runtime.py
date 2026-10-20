from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, or_, select, update

from app.core.crypto import TokenEncryptor
from app.db.models import (
    ModelSource,
    ModelSourceModel,
    ModelSourceOAuthCredential,
    ModelSourceQuotaObservation,
    ModelSourceQuotaWindow,
)
from app.db.session import detach_session_objects, get_background_session
from app.modules.model_sources.provider_schemas import configuration_fingerprint
from app.modules.model_sources.providers import claude, deepseek, kimi, nanogpt, official_apis, zai
from app.modules.model_sources.providers.base import ProviderError, ProviderSnapshot, utcnow
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.schemas import ModelSourceResponse
from app.modules.model_sources.service import _to_response

CLAIM_TTL = timedelta(seconds=90)
REFRESH_MARGIN = timedelta(minutes=5)


async def load_source(source_id: str) -> ModelSource:
    async with get_background_session() as session:
        source = await session.get(ModelSource, source_id)
        if source is None:
            raise ProviderError("Model source not found")
        detach_session_objects(session)
        return source


def _require_claude(source: ModelSource) -> None:
    if (
        source.provider != "claude"
        or source.auth_type != "claude_oauth"
        or source.base_url != "https://api.anthropic.com"
    ):
        raise ProviderError("Claude OAuth requires the fixed Claude provider preset")


async def install_credential(
    source_id: str,
    pair: claude.TokenPair,
    *,
    expected_generation: int | None,
    expected_configuration: str,
    expires_at: datetime | None = None,
) -> None:
    """Replace a credential atomically; pending refreshes cannot win after reauth."""
    encryptor = TokenEncryptor()
    async with get_background_session() as session:
        result = await session.execute(
            update(ModelSource)
            .where(
                ModelSource.id == source_id,
                ModelSource.provider == "claude",
                ModelSource.auth_type == "claude_oauth",
                ModelSource.base_url == "https://api.anthropic.com",
            )
            .values(
                entitlement_status="unverified",
                entitlement_verified_at=None,
                entitlement_detail="Credential connected; native plan verification pending",
                quota_error=None,
                extra_usage_enabled=None,
            )
        )
        if result.rowcount != 1:
            raise ProviderError("Source changed during authorization; start a new sign-in")
        source = await session.get(ModelSource, source_id)
        if source is None or configuration_fingerprint(source) != expected_configuration:
            raise ProviderError("Source configuration changed during authorization; start a new sign-in")
        credential = await session.get(ModelSourceOAuthCredential, source_id, with_for_update=True)
        if (credential.generation if credential else None) != expected_generation:
            raise ProviderError("Credential changed during authorization; start a new sign-in")
        repository = ModelSourcesRepository(session)
        previous_history_ids = await repository.quota_history_component_ids(source_id)
        values = dict(
            access_token_encrypted=encryptor.encrypt(pair.access_token.get_secret_value()),
            refresh_token_encrypted=encryptor.encrypt(pair.refresh_token.get_secret_value())
            if pair.refresh_token
            else None,
            expires_at=expires_at if expires_at is not None else utcnow() + timedelta(seconds=pair.expires_in),
            generation=(expected_generation or 0) + 1,
            refresh_claim_owner=None,
            refresh_claim_expires_at=None,
            account_uuid=None,
            organization_uuid=None,
            account_label=None,
            subscription_type=None,
        )
        if credential is None:
            session.add(ModelSourceOAuthCredential(source_id=source_id, **values))
        else:
            for key, value in values.items():
                setattr(credential, key, value)
        # Existing observations describe the previous principal, never the new one.
        source = await session.get(ModelSource, source_id)
        if source is not None:
            source.quota_windows.clear()
            current_history_ids = await repository.quota_history_component_ids(source_id)
            await repository.clear_quota_history(previous_history_ids | current_history_ids)
            for model in source.models:
                model.subscription_eligible = False
        await session.commit()


async def _refresh_failure(source_id: str, generation: int, owner: str, error: ProviderError) -> None:
    async with get_background_session() as session:
        # Lock the parent first, matching management-write ordering.
        locked = await session.execute(
            update(ModelSource)
            .where(
                ModelSource.id == source_id,
                exists(
                    select(ModelSourceOAuthCredential.source_id).where(
                        ModelSourceOAuthCredential.source_id == source_id,
                        ModelSourceOAuthCredential.generation == generation,
                        ModelSourceOAuthCredential.refresh_claim_owner == owner,
                    )
                ),
            )
            .values(
                quota_error=str(error),
                **(
                    {"entitlement_status": "reauth_required", "entitlement_verified_at": None}
                    if error.permanent
                    else {}
                ),
            )
        )
        if locked.rowcount == 1:
            await session.execute(
                update(ModelSourceOAuthCredential)
                .where(
                    ModelSourceOAuthCredential.source_id == source_id,
                    ModelSourceOAuthCredential.generation == generation,
                    ModelSourceOAuthCredential.refresh_claim_owner == owner,
                )
                .values(
                    refresh_claim_owner=None,
                    refresh_claim_expires_at=None,
                    generation=generation + 1 if error.permanent else generation,
                )
            )
        await session.commit()


async def fresh_credential(source_id: str) -> ModelSource:
    """Per-source DB single-flight with generation+owner compare-and-swap.

    No DB transaction spans HTTP. Claims last longer than the bounded request;
    contenders reread rather than rotate the same refresh token. Cancellation
    leaves the expiring claim in place because the token may already be spent.
    """
    deadline = asyncio.get_running_loop().time() + 35
    encryptor = TokenEncryptor()
    while True:
        source = await load_source(source_id)
        _require_claude(source)
        credential = source.oauth_credential
        if credential is None or source.entitlement_status == "reauth_required":
            raise ProviderError("Claude credential requires sign-in", permanent=True)
        if credential.expires_at > utcnow() + REFRESH_MARGIN:
            return source
        if credential.refresh_token_encrypted is None:
            if credential.expires_at > utcnow():
                return source
            async with get_background_session() as session:
                await session.execute(
                    update(ModelSource)
                    .where(*_snapshot_guard(source))
                    .values(
                        entitlement_status="reauth_required",
                        entitlement_verified_at=None,
                        quota_error="Claude access token expired and no refresh token is available",
                    )
                )
                await session.commit()
            raise ProviderError("Claude access token expired and no refresh token is available", permanent=True)
        owner = secrets.token_hex(16)
        generation = credential.generation
        async with get_background_session() as session:
            claim = await session.execute(
                update(ModelSourceOAuthCredential)
                .where(
                    ModelSourceOAuthCredential.source_id == source_id,
                    ModelSourceOAuthCredential.generation == generation,
                    or_(
                        ModelSourceOAuthCredential.refresh_claim_owner.is_(None),
                        ModelSourceOAuthCredential.refresh_claim_expires_at < utcnow(),
                    ),
                )
                .values(refresh_claim_owner=owner, refresh_claim_expires_at=utcnow() + CLAIM_TTL)
            )
            await session.commit()
            won = claim.rowcount == 1
        if not won:
            if asyncio.get_running_loop().time() >= deadline:
                raise ProviderError("Claude credential refresh is in progress; retry shortly")
            await asyncio.sleep(0.1)
            continue
        try:
            pair = await claude.refresh(encryptor.decrypt(credential.refresh_token_encrypted))
        except ProviderError as error:
            await _refresh_failure(source_id, generation, owner, error)
            # Transient failures do not revoke or erase the last valid token.
            if not error.permanent and credential.expires_at > utcnow():
                return await load_source(source_id)
            raise
        async with get_background_session() as session:
            parent = await session.execute(
                update(ModelSource)
                .where(
                    ModelSource.id == source_id,
                    ModelSource.provider == "claude",
                    ModelSource.auth_type == "claude_oauth",
                    exists(
                        select(ModelSourceOAuthCredential.source_id).where(
                            ModelSourceOAuthCredential.source_id == source_id,
                            ModelSourceOAuthCredential.generation == generation,
                            ModelSourceOAuthCredential.refresh_claim_owner == owner,
                        )
                    ),
                )
                .values(quota_error=None)
            )
            if parent.rowcount == 1:
                result = await session.execute(
                    update(ModelSourceOAuthCredential)
                    .where(
                        ModelSourceOAuthCredential.source_id == source_id,
                        ModelSourceOAuthCredential.generation == generation,
                        ModelSourceOAuthCredential.refresh_claim_owner == owner,
                    )
                    .values(
                        access_token_encrypted=encryptor.encrypt(pair.access_token.get_secret_value()),
                        refresh_token_encrypted=encryptor.encrypt(pair.refresh_token.get_secret_value())
                        if pair.refresh_token
                        else None,
                        expires_at=utcnow() + timedelta(seconds=pair.expires_in),
                        generation=generation + 1,
                        refresh_claim_owner=None,
                        refresh_claim_expires_at=None,
                    )
                )
                if result.rowcount != 1:
                    raise ProviderError("Claude credential changed during refresh")
            await session.commit()
        # Always reread: a concurrent import/configuration change may have won.
        return await load_source(source_id)


def _assert_selected_snapshot(source: ModelSource, current: ModelSource, protocol: str) -> None:
    configuration = (
        "provider",
        "auth_type",
        "base_url",
        "messages_base_url",
        "billing_mode",
        "quota_group",
        "supports_messages",
        "supports_chat_completions",
        "supports_responses",
        "api_key_encrypted",
        "key_type",
        "routing_policy",
        "api_fallback_only",
        "limit_warmup_enabled",
    )
    if not current.is_enabled or any(getattr(source, name) != getattr(current, name) for name in configuration):
        raise ProviderError("Source configuration changed after selection; select again")
    selected_models = {
        (
            entry.model, entry.upstream_model, entry.is_enabled,
            entry.subscription_eligible, entry.subscription_restricted,
        )
        for entry in source.models
    }
    current_models = {
        (
            entry.model, entry.upstream_model, entry.is_enabled,
            entry.subscription_eligible, entry.subscription_restricted,
        )
        for entry in current.models
    }
    if selected_models != current_models:
        raise ProviderError("Source model authorization changed after selection; select again")
    capability = {
        "messages": current.supports_messages,
        "chat_completions": current.supports_chat_completions,
        "responses": current.supports_responses,
    }.get(protocol)
    if capability is False:
        raise ProviderError("Source does not support the selected protocol")
    if current.entitlement_status in {"blocked", "reauth_required"}:
        raise ProviderError("Source is no longer eligible after selection")
    if current.cooldown_until is not None and current.cooldown_until > utcnow():
        raise ProviderError("Source entered cooldown after selection")
    if source.billing_mode in {"subscription", "coding_plan"}:
        if (
            current.entitlement_status != "verified"
            or current.extra_usage_enabled is not False
            or current.quota_error is not None
            or current.quota_checked_at is None
            or current.quota_checked_at < utcnow() - timedelta(minutes=5)
            or current.entitlement_verified_at is None
            or current.entitlement_verified_at < utcnow() - timedelta(minutes=5)
            or current.quota_checked_at != source.quota_checked_at
            or current.entitlement_verified_at != source.entitlement_verified_at
        ):
            raise ProviderError("Source entitlement or quota changed after selection; select again")
    selected = source.oauth_credential
    fresh = current.oauth_credential
    if selected is not None and (
        fresh is None
        or selected.account_uuid != fresh.account_uuid
        or selected.organization_uuid != fresh.organization_uuid
    ):
        raise ProviderError("Source principal changed after selection; select again")


async def upstream_headers(
    source: ModelSource,
    *,
    protocol: str,
    incoming_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    incoming = {key.lower(): value for key, value in (incoming_headers or {}).items()}
    result: dict[str, str] = {}
    if protocol == "messages":
        result["anthropic-version"] = incoming.get("anthropic-version", "2023-06-01")
        if "anthropic-beta" in incoming:
            result["anthropic-beta"] = incoming["anthropic-beta"]
    if source.auth_type == "claude_oauth":
        current = await fresh_credential(source.id)
        credential = current.oauth_credential
        if credential is None:
            raise ProviderError("Claude credential requires sign-in", permanent=True)
        _assert_selected_snapshot(source, current, protocol)
        # Update the detached selected source so identity/body and failure CAS use
        # precisely the generation whose bearer was returned.
        source.oauth_credential = credential
        result["authorization"] = "Bearer " + TokenEncryptor().decrypt(credential.access_token_encrypted)
        betas = [item.strip() for item in result.get("anthropic-beta", "").split(",") if item.strip()]
        if claude.OAUTH_BETA not in betas:
            betas.append(claude.OAUTH_BETA)
        result["anthropic-beta"] = ",".join(betas)
        return result
    if source.auth_type == "api_key" and source.api_key_encrypted:
        if source.provider != "custom":
            current = await load_source(source.id)
            _assert_selected_snapshot(source, current, protocol)
        key = TokenEncryptor().decrypt(source.api_key_encrypted)
        if protocol == "messages" and source.provider in {"custom", "anthropic"}:
            result["x-api-key"] = key
            if source.provider == "anthropic" and "anthropic-beta" in result:
                # Clients may shape both subscription and paid requests through
                # the same Anthropic provider; OAuth authority is source-owned.
                betas = [
                    item.strip() for item in result["anthropic-beta"].split(",")
                    if item.strip() and item.strip() != claude.OAUTH_BETA
                ]
                if betas:
                    result["anthropic-beta"] = ",".join(betas)
                else:
                    result.pop("anthropic-beta")
        else:
            # GLM monitor's raw-key auth is metadata-only. Native GLM/Kimi
            # inference uses Bearer even on its Anthropic-compatible path.
            result["authorization"] = "Bearer " + key
    return result


def _snapshot_guard(source: ModelSource):
    conditions = [
        ModelSource.id == source.id,
        ModelSource.provider == source.provider,
        ModelSource.auth_type == source.auth_type,
        ModelSource.api_key_encrypted == source.api_key_encrypted,
        ModelSource.base_url == source.base_url,
        ModelSource.is_enabled == source.is_enabled,
        ModelSource.billing_mode == source.billing_mode,
        ModelSource.messages_base_url == source.messages_base_url,
        ModelSource.supports_messages == source.supports_messages,
        ModelSource.supports_chat_completions == source.supports_chat_completions,
        ModelSource.supports_responses == source.supports_responses,
        ModelSource.quota_group == source.quota_group,
        ModelSource.key_type == source.key_type,
        ModelSource.routing_policy == source.routing_policy,
        ModelSource.api_fallback_only == source.api_fallback_only,
        ModelSource.limit_warmup_enabled == source.limit_warmup_enabled,
    ]
    if source.oauth_credential:
        conditions.append(
            exists(
                select(ModelSourceOAuthCredential.source_id).where(
                    ModelSourceOAuthCredential.source_id == source.id,
                    ModelSourceOAuthCredential.generation == source.oauth_credential.generation,
                )
            )
        )
    return conditions


async def _persist_snapshot(source: ModelSource, snapshot: ProviderSnapshot) -> None:
    now = utcnow()
    credential = source.oauth_credential
    history_cutover = (
        snapshot.quota_group is not None and snapshot.quota_group != source.quota_group
    ) or (
        credential is not None
        and (credential.account_uuid, credential.organization_uuid) != (
            snapshot.account_uuid, snapshot.organization_uuid,
        )
    )
    async with get_background_session() as session:
        applied = await session.execute(
            update(ModelSource)
            .where(*_snapshot_guard(source))
            .values(
                billing_mode=snapshot.billing_mode,
                entitlement_status=snapshot.entitlement_status,
                entitlement_verified_at=now if snapshot.entitlement_status == "verified" else None,
                entitlement_detail=snapshot.detail,
                extra_usage_enabled=snapshot.extra_usage_enabled,
                quota_group=snapshot.quota_group if snapshot.quota_group is not None else source.quota_group,
                quota_checked_at=now,
                quota_error=None,
                detected_key_type=snapshot.detected_key_type,
                health_status="healthy",
                **({
                    "api_balance_json": json.dumps(snapshot.balances),
                    "api_balance_observed_at": now,
                    "api_balance_is_available": snapshot.balance_is_available,
                } if source.provider == "deepseek" else {}),
            )
        )
        if applied.rowcount != 1:
            raise ProviderError("Source changed during quota refresh; refresh again")
        row = await session.get(ModelSource, source.id)
        if row is None:
            raise ProviderError("Model source not found")
        repository = ModelSourcesRepository(session)
        previous_history_ids = (
            await repository.quota_history_component_ids(source.id, previous_source=source)
            if history_cutover else set()
        )
        credential = row.oauth_credential
        if credential is not None:
            credential.account_uuid = snapshot.account_uuid
            credential.organization_uuid = snapshot.organization_uuid
        if history_cutover:
            current_history_ids = await repository.quota_history_component_ids(source.id)
            await repository.clear_quota_history(previous_history_ids | current_history_ids)
        row.quota_windows.clear()
        await session.flush()
        row.quota_windows.extend(
            ModelSourceQuotaWindow(
                window_key=window.window_key,
                model=window.model,
                used_ratio=window.used_ratio,
                limit_value=window.limit_value,
                used_value=window.used_value,
                resets_at=window.resets_at,
                observed_at=now,
            )
            for window in snapshot.windows
        )
        session.add_all([
            ModelSourceQuotaObservation(
                source_id=source.id,
                window_key=window.window_key,
                model=window.model,
                used_ratio=window.used_ratio,
                limit_value=window.limit_value,
                used_value=window.used_value,
                resets_at=window.resets_at,
                observed_at=now,
            )
            for window in snapshot.windows
        ])
        await session.execute(
            delete(ModelSourceQuotaObservation).where(
                ModelSourceQuotaObservation.source_id == source.id,
                ModelSourceQuotaObservation.observed_at < now - timedelta(days=30),
            )
        )
        metered_api = source.provider in {"openai", "anthropic"}
        discovered = {item.upstream_model or item.model: item for item in snapshot.models}
        existing = {model.upstream_model or model.model: model for model in row.models}
        for model in row.models:
            item = discovered.get(model.upstream_model or model.model)
            model.subscription_eligible = bool(
                item and item.subscription_eligible and not model.subscription_restricted
            )
            if item:
                if item.context_window is not None and (not metered_api or model.context_window is None):
                    model.context_window = item.context_window
                if item.max_output_tokens is not None and (not metered_api or model.max_output_tokens is None):
                    model.max_output_tokens = item.max_output_tokens
                if item.supports_vision is not None and not metered_api:
                    model.supports_vision = item.supports_vision
                if item.supports_tools is not None and not metered_api:
                    model.supports_tools = item.supports_tools
                if item.raw_metadata_json:
                    # Preserve the existing capability SSOT/operator restrictions;
                    # provider discovery adds evidence, not unrelated overrides.
                    previous = json.loads(model.raw_metadata_json) if model.raw_metadata_json else {}
                    native = json.loads(item.raw_metadata_json)
                    if metered_api:
                        # API metadata remains operator-editable across refreshes.
                        merged = {**native, **previous}
                    else:
                        merged = (
                            {**previous, **native}
                            if previous.get("provider_catalog") is True else {**native, **previous}
                        )
                        merged["capabilities"] = native.get("capabilities", {})
                    model.raw_metadata_json = json.dumps(merged)
        for item in snapshot.models:
            if (item.upstream_model or item.model) in existing:
                continue
            row.models.append(
                ModelSourceModel(
                    model=item.model,
                    upstream_model=item.upstream_model,
                    display_name=item.display_name,
                    context_window=item.context_window,
                    max_output_tokens=item.max_output_tokens,
                    subscription_eligible=item.subscription_eligible,
                    supports_streaming=True,
                    supports_tools=item.supports_tools is True,
                    supports_vision=item.supports_vision is True,
                    raw_metadata_json=item.raw_metadata_json or json.dumps({"provider_catalog": True}),
                )
            )
        if row.oauth_credential:
            row.oauth_credential.account_label = snapshot.account_label
            row.oauth_credential.subscription_type = snapshot.subscription_type
        await session.commit()


async def refresh_source_quota(source_id: str) -> ModelSourceResponse:
    source = await load_source(source_id)
    if source.provider == "custom":
        raise ProviderError("Custom sources have no subscription quota endpoint")
    try:
        if source.provider == "claude":
            source = await fresh_credential(source_id)
            credential = source.oauth_credential
            if credential is None:
                raise ProviderError("Claude credential requires sign-in", permanent=True)
            snapshot = await claude.inspect(TokenEncryptor().decrypt(credential.access_token_encrypted))
        else:
            if source.auth_type != "api_key" or not source.api_key_encrypted:
                raise ProviderError("Connect a provider API credential before refreshing quota")
            adapters = {
                "zai": zai.inspect,
                "kimi": kimi.inspect,
                "nanogpt": nanogpt.inspect,
                "deepseek": deepseek.inspect,
                "openai": official_apis.inspect_openai,
                "anthropic": official_apis.inspect_anthropic,
            }
            adapter = adapters.get(source.provider)
            if adapter is None:
                raise ProviderError("Unsupported subscription provider")
            snapshot = await adapter(TokenEncryptor().decrypt(source.api_key_encrypted))
            if source.provider == "zai" and source.key_type == "standard":
                # Declaration is an operator restriction, never verification.
                # Standard keys can reach coding paths while spending balance.
                snapshot.entitlement_status = "blocked"
                snapshot.detail = (
                    "Declared GLM Standard key is blocked: coding endpoint access can consume API balance; "
                    "use a verified Coding key"
                )
                for model in snapshot.models:
                    model.subscription_eligible = False
        await _persist_snapshot(source, snapshot)
        if source.provider == "claude":
            from app.modules.model_sources.subscription_warmup import observe_warmup_window

            await observe_warmup_window(await load_source(source_id))
    except ProviderError as error:
        async with get_background_session() as session:
            await session.execute(
                update(ModelSource)
                .where(*_snapshot_guard(source))
                .values(
                    quota_checked_at=utcnow(),
                    quota_error=str(error),
                    health_status="error",
                    **(
                        {"entitlement_status": "reauth_required", "entitlement_verified_at": None}
                        if error.permanent
                        else {}
                    ),
                )
            )
            await session.commit()
        # Safe persisted observations/error let existing UI show real failures,
        # not a misleading successful metadata refresh.
    return _to_response(await load_source(source_id))


async def refresh_subscription_sources() -> None:
    """One sequential, zero-inference cycle. Main owns leader gating/lifecycle."""
    async with get_background_session() as session:
        ids = list(
            (
                await session.scalars(
                    select(ModelSource.id).where(
                        ModelSource.is_enabled.is_(True),
                        ModelSource.provider != "custom",
                    )
                )
            ).all()
        )
    for source_id in ids:
        try:
            await refresh_source_quota(source_id)
        except ProviderError:
            # A concurrent deletion/configuration change is local to that source.
            continue


async def on_source_rejection(source: ModelSource, status: int, headers: Mapping[str, str]) -> None:
    """Persist only the selected credential generation's explicit rejection.

    This never translates provider errors, retries inference, or records an
    upstream body. A policy/region 403 is not a revoked OAuth refresh token.
    """
    if status not in {401, 403, 429}:
        return
    conditions = [
        ModelSource.id == source.id,
        ModelSource.provider == source.provider,
        ModelSource.auth_type == source.auth_type,
        ModelSource.base_url == source.base_url,
        ModelSource.api_key_encrypted == source.api_key_encrypted,
    ]
    if source.oauth_credential:
        conditions.append(
            exists(
                select(ModelSourceOAuthCredential.source_id).where(
                    ModelSourceOAuthCredential.source_id == source.id,
                    ModelSourceOAuthCredential.generation == source.oauth_credential.generation,
                )
            )
        )
    values: dict = {}
    if status == 401:
        values.update(
            entitlement_status="reauth_required",
            entitlement_verified_at=None,
            quota_error="Provider rejected the selected credential (HTTP 401)",
        )
    elif status == 403:
        values.update(
            entitlement_status="blocked",
            entitlement_verified_at=None,
            quota_error="Provider refused the selected source (HTTP 403); check provider policy and region",
        )
    else:
        normalized = {key.lower(): value for key, value in headers.items()}
        try:
            retry = max(1, min(int(normalized.get("retry-after", "60")), 86400))
        except (TypeError, ValueError):
            retry = 60
        values.update(
            cooldown_until=utcnow() + timedelta(seconds=retry),
            quota_error="Provider plan limit reached (HTTP 429); metadata refresh required",
        )
    async with get_background_session() as session:
        await session.execute(update(ModelSource).where(*conditions).values(**values))
        await session.commit()
