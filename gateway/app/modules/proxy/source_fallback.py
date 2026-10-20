"""Codex-family API participation is decided before dispatch, never after failure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config.settings_cache import get_settings_cache
from app.core.openai.model_registry import get_model_registry
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy.load_balancer import effective_account_concurrency_caps

if TYPE_CHECKING:
    from app.modules.proxy.service import ProxyService


async def codex_api_fallback_tier(
    service: ProxyService,
    model: str | None,
    api_key: ApiKeyData | None,
    *,
    service_tier: str | None = None,
    previous_response_id: str | None = None,
    required_account_id: str | None = None,
    source_route_excluded: bool = False,
    hard_continuity_anchor: bool = False,
    require_security_work_authorized: bool = False,
) -> int | None:
    """Return native's eligible policy rank, 3 for unavailable, None if fenced.

    The selector admits fallback-only APIs only at 3. Opted-out APIs must beat
    a native account's policy strictly; subscriptions win ties. This is an
    advisory observation, not a reservation or a post-dispatch failover rule.
    Unknown continuation ownership never permits account-owned turn replay.
    """
    if (
        model is None
        or source_route_excluded
        or hard_continuity_anchor
        or previous_response_id is not None
        or required_account_id is not None
        or require_security_work_authorized
        or model not in get_model_registry().get_models_with_fallback()
        or (api_key is not None and api_key.billing_policy != "any")
    ):
        return None
    settings = await get_settings_cache().get()
    rank = await service._load_balancer.eligible_account_routing_rank(
        model=model,
        service_tier=service_tier,
        account_ids=(
            api_key.assigned_account_ids
            if api_key is not None and api_key.account_assignment_scope_enabled
            else None
        ),
        concurrency_caps=effective_account_concurrency_caps(settings),
        dashboard_settings=settings,
    )
    return 3 if rank is None else rank
