"""Standalone dashboard batch endpoint; token estimates never replace credits."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dashboard_access import Permission
from app.core.auth.dependencies import (
    require_dashboard_permission,
    set_dashboard_error_format,
    validate_dashboard_session,
)
from app.core.utils.time import utcnow
from app.db.session import get_session
from app.modules.quota_estimates.batch import estimate_quotas_batch
from app.modules.quota_estimates.schemas import QuotaEstimatesResponse

router = APIRouter(
    prefix="/api/quota-estimates",
    tags=["dashboard"],
    dependencies=[
        Depends(validate_dashboard_session),
        Depends(set_dashboard_error_format),
        # As with existing account/dashboard views, own-scoped account reads
        # are not supported. The shared dependency requires scope ALL, which
        # also protects source identities and shared-principal aggregates.
        Depends(require_dashboard_permission(Permission.ACCOUNTS_READ)),
    ],
)


@router.get("/", response_model=QuotaEstimatesResponse)
async def get_quota_estimates(
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> QuotaEstimatesResponse:
    response.headers["Cache-Control"] = "no-store"
    return await estimate_quotas_batch(session, now=utcnow())
