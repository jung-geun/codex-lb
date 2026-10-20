from __future__ import annotations

from sqlalchemy import and_, case, func, literal, or_

from app.db.models import RequestLog

_INTERNAL_LIMIT_WARMUP_SOURCE = "limit_warmup"
_INTERNAL_WARMUP_REQUEST_KINDS = ("warmup", "limit_warmup")
MISSING_USERAGENT_GROUP = "Missing User-Agent"


def _useragent_group_filter_clause(useragent_group: str | None):
    if not useragent_group:
        return None
    if useragent_group == MISSING_USERAGENT_GROUP:
        return RequestLog.useragent_group.is_(None)
    return RequestLog.useragent_group == useragent_group


def _normal_traffic_clause():
    return and_(
        or_(RequestLog.source.is_(None), RequestLog.source != _INTERNAL_LIMIT_WARMUP_SOURCE),
        or_(
            RequestLog.request_kind.is_(None),
            RequestLog.request_kind.not_in(_INTERNAL_WARMUP_REQUEST_KINDS),
        ),
    )


def account_selector_expr(account_id, model_source_id):
    """Keep source ids disjoint from the legacy Codex account namespace."""
    return case((model_source_id.is_not(None), literal("source:") + model_source_id), else_=account_id)


def raw_account_type_expr():
    return case(
        (RequestLog.model_source_id.is_(None), "codex"),
        else_=func.coalesce(func.nullif(RequestLog.provider, ""), "other"),
    )


def raw_identity_conditions(account_ids: list[str] | None = None, account_types: list[str] | None = None):
    conditions = []
    if account_ids:
        conditions.append(account_selector_expr(RequestLog.account_id, RequestLog.model_source_id).in_(account_ids))
    if account_types:
        conditions.append(raw_account_type_expr().in_(account_types))
    return conditions
