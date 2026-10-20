from __future__ import annotations

import math

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from app.core.audit.service import AuditActor, AuditService, AuditTarget
from app.core.auth.dashboard_access import DashboardPrincipal, Permission
from app.core.auth.dependencies import (
    require_dashboard_permission,
    require_dashboard_write_access,
    set_dashboard_error_format,
    validate_dashboard_session,
)
from app.core.exceptions import DashboardBadRequestError, DashboardConflictError, DashboardNotFoundError
from app.dependencies import ModelSourcesContext, get_model_sources_context
from app.modules.model_sources.oauth import flows, session_binding
from app.modules.model_sources.provider_schemas import (
    CredentialEnvelope,
    CredentialImportRequest,
    OAuthCompleteRequest,
    OAuthStartResponse,
    OAuthStatusResponse,
    ProviderPreset,
    configuration_fingerprint,
    presets,
)
from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import ProviderError, parse_time
from app.modules.model_sources.schemas import (
    ModelSourceApiUsageResponse,
    ModelSourceCreateRequest,
    ModelSourceQuotaHistoryResponse,
    ModelSourceResponse,
    ModelSourcesResponse,
    ModelSourceUpdateRequest,
    ModelSourceUsageResponse,
)
from app.modules.model_sources.service import (
    ModelSourceInUseError,
    ModelSourceNotFoundError,
    ModelSourceValidationError,
)
from app.modules.model_sources.subscription_runtime import (
    _require_claude,
    install_credential,
    load_source,
    refresh_source_quota,
)

router = APIRouter(
    prefix="/api/model-sources",
    tags=["dashboard"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


def _provider_bad_request(error: ProviderError) -> DashboardBadRequestError:
    return DashboardBadRequestError(str(error), code="provider_connection_error")


def _audit_connection(request: Request, principal: DashboardPrincipal, source_id: str, action: str) -> None:
    AuditService.log_async(
        action,
        actor_ip=request.client.host if request.client else None,
        actor=AuditActor.from_principal(principal),
        target=AuditTarget("model_source", source_id),
        details={"source_id": source_id},
    )


@router.get("/presets", response_model=list[ProviderPreset])
async def provider_presets() -> list[ProviderPreset]:
    return presets()


@router.post("/{source_id}/oauth/start", response_model=OAuthStartResponse)
async def start_source_oauth(
    source_id: str,
    request: Request,
    response: Response,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> OAuthStartResponse:
    try:
        source = await load_source(source_id)
        _require_claude(source)
        result = flows.start(
            source_id,
            session_binding(request, principal, response),
            generation=source.oauth_credential.generation if source.oauth_credential else None,
            configuration=configuration_fingerprint(source),
        )
    except ProviderError as error:
        raise _provider_bad_request(error) from error
    _audit_connection(request, principal, source_id, "model_source_oauth_started")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{source_id}/oauth/status", response_model=OAuthStatusResponse)
async def source_oauth_status(
    source_id: str,
    request: Request,
    response: Response,
    flow_id: str = Query(alias="flowId", min_length=1, max_length=128),
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> OAuthStatusResponse:
    try:
        result = flows.status(source_id, flow_id, session_binding(request, principal))
    except ProviderError as error:
        raise _provider_bad_request(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/{source_id}/oauth/flows/{flow_id}", status_code=204)
async def cancel_source_oauth(
    source_id: str,
    flow_id: str,
    request: Request,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> Response:
    try:
        flows.cancel(source_id, flow_id, session_binding(request, principal))
    except ProviderError as error:
        raise _provider_bad_request(error) from error
    _audit_connection(request, principal, source_id, "model_source_oauth_cancelled")
    return Response(status_code=204)


@router.post("/{source_id}/oauth/complete", response_model=ModelSourceResponse)
async def complete_source_oauth(
    source_id: str,
    request: Request,
    payload: OAuthCompleteRequest,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> ModelSourceResponse:
    flow = None
    try:
        binding = session_binding(request, principal)
        flow, pair = await flows.exchange(source_id, payload.flow_id, binding, payload.code.get_secret_value())
        await install_credential(
            source_id,
            pair,
            expected_generation=flow.credential_generation,
            expected_configuration=flow.source_configuration,
        )
        flow.status = "completed"
    except ProviderError as error:
        if flow is not None:
            flow.status = "failed"
            flow.error = "Source changed during authorization; start a new sign-in"
        raise _provider_bad_request(error) from error
    _audit_connection(request, principal, source_id, "model_source_oauth_connected")
    return await refresh_source_quota(source_id)


@router.post("/{source_id}/credentials/import", response_model=ModelSourceResponse)
async def import_source_credential(
    source_id: str,
    request: Request,
    payload: CredentialImportRequest,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> ModelSourceResponse:
    try:
        source = await load_source(source_id)
        _require_claude(source)
        credential = (
            payload.credentials.claudeAiOauth
            if isinstance(payload.credentials, CredentialEnvelope)
            else payload.credentials
        )
        expires = parse_time(credential.expiresAt)
        if expires is None or not math.isfinite(credential.expiresAt):
            raise ProviderError("Imported credential has an invalid expiration")
        # Preserve an expired credential so the DB-claimed refresh path (not
        # an unowned import-side refresh) performs its first rotation.
        pair = claude.TokenPair(
            access_token=credential.accessToken, refresh_token=credential.refreshToken, expires_in=1
        )
        await install_credential(
            source_id,
            pair,
            expected_generation=source.oauth_credential.generation if source.oauth_credential else None,
            expected_configuration=configuration_fingerprint(source),
            expires_at=expires,
        )
    except ProviderError as error:
        raise _provider_bad_request(error) from error
    _audit_connection(request, principal, source_id, "model_source_credential_imported")
    return await refresh_source_quota(source_id)


@router.post("/{source_id}/quota", response_model=ModelSourceResponse)
async def refresh_source_quota_route(
    source_id: str,
    request: Request,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> ModelSourceResponse:
    try:
        result = await refresh_source_quota(source_id)
    except ProviderError as error:
        raise _provider_bad_request(error) from error
    _audit_connection(request, principal, source_id, "model_source_quota_refreshed")
    return result


@router.post("/{source_id}/warmup", response_model=ModelSourceResponse)
async def warm_source_route(
    source_id: str,
    request: Request,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
) -> ModelSourceResponse:
    from app.modules.model_sources.subscription_warmup import warm_source

    try:
        result = await warm_source(source_id)
    except ProviderError as error:
        raise _provider_bad_request(error) from error
    _audit_connection(request, principal, source_id, "model_source_warmup_requested")
    return result


@router.get(
    "/",
    response_model=ModelSourcesResponse,
    dependencies=[Depends(require_dashboard_permission(Permission.ACCOUNTS_READ))],
)
async def list_model_sources(
    response: Response,
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> ModelSourcesResponse:
    response.headers["Cache-Control"] = "no-store"
    return ModelSourcesResponse(sources=await context.service.list_sources())


@router.get(
    "/{source_id}/usage",
    response_model=ModelSourceUsageResponse,
    dependencies=[Depends(require_dashboard_permission(Permission.ACCOUNTS_READ))],
)
async def model_source_usage(
    source_id: str,
    response: Response,
    days: int = Query(default=1, ge=1, le=30),
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> ModelSourceUsageResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await context.service.source_usage(source_id, days=days)
    except ModelSourceNotFoundError as exc:
        raise DashboardNotFoundError(str(exc)) from exc


@router.get(
    "/{source_id}/api-usage",
    response_model=ModelSourceApiUsageResponse,
    dependencies=[Depends(require_dashboard_permission(Permission.ACCOUNTS_READ))],
)
async def model_source_api_usage(
    source_id: str,
    response: Response,
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> ModelSourceApiUsageResponse:
    from app.modules.model_sources.api_usage import api_usage_response

    response.headers["Cache-Control"] = "no-store"
    source = await context.repository.get_by_id(source_id)
    if source is None:
        raise DashboardNotFoundError(f"Model source not found: {source_id}")
    return api_usage_response(source)


@router.get(
    "/{source_id}/quota-history",
    response_model=ModelSourceQuotaHistoryResponse,
    dependencies=[Depends(require_dashboard_permission(Permission.ACCOUNTS_READ))],
)
async def model_source_quota_history(
    source_id: str,
    response: Response,
    days: int = Query(default=7, ge=1, le=30),
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> ModelSourceQuotaHistoryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await context.service.source_quota_history(source_id, days=days)
    except ModelSourceNotFoundError as exc:
        raise DashboardNotFoundError(str(exc)) from exc


@router.post("/", response_model=ModelSourceResponse)
async def create_model_source(
    request: Request,
    payload: ModelSourceCreateRequest = Body(...),
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> ModelSourceResponse:
    try:
        created = await context.service.create_source(payload)
    except ModelSourceValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_model_source_payload") from exc
    AuditService.log_async(
        "model_source_created",
        actor_ip=request.client.host if request.client else None,
        actor=AuditActor.from_principal(principal),
        target=AuditTarget("model_source", created.id),
        details={"source_id": created.id},
    )
    return created


@router.patch("/{source_id}", response_model=ModelSourceResponse)
async def update_model_source(
    request: Request,
    source_id: str,
    payload: ModelSourceUpdateRequest = Body(...),
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> ModelSourceResponse:
    try:
        updated = await context.service.update_source(source_id, payload)
    except ModelSourceNotFoundError as exc:
        raise DashboardNotFoundError(str(exc)) from exc
    except ModelSourceValidationError as exc:
        raise DashboardBadRequestError(str(exc), code="invalid_model_source_payload") from exc
    AuditService.log_async(
        "model_source_updated",
        actor_ip=request.client.host if request.client else None,
        actor=AuditActor.from_principal(principal),
        target=AuditTarget("model_source", updated.id),
        details={"source_id": updated.id},
    )
    return updated


@router.delete("/{source_id}")
async def delete_model_source(
    request: Request,
    source_id: str,
    principal: DashboardPrincipal = Depends(require_dashboard_write_access),
    context: ModelSourcesContext = Depends(get_model_sources_context),
) -> Response:
    try:
        await context.service.delete_source(source_id)
    except ModelSourceNotFoundError as exc:
        raise DashboardNotFoundError(str(exc)) from exc
    except ModelSourceInUseError as exc:
        raise DashboardConflictError(str(exc), code="model_source_in_use") from exc
    AuditService.log_async(
        "model_source_deleted",
        actor_ip=request.client.host if request.client else None,
        actor=AuditActor.from_principal(principal),
        target=AuditTarget("model_source", source_id),
        details={"source_id": source_id},
    )
    return Response(status_code=204)
