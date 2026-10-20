"""Native, read-only NanoGPT inspection (no inference or balance probes).

Contracts:
https://docs.nano-gpt.com/api-reference/endpoint/subscription-usage
https://docs.nano-gpt.com/api-reference/management-api#read-subscription-usage
https://docs.nano-gpt.com/api-reference/endpoint/models#endpoint-variants
https://docs.nano-gpt.com/api-reference/miscellaneous/billing-override

The website host is documented for these endpoints. Its catalog can differ from
api.nano-gpt.com during rollouts; this adapter deliberately never switches hosts.
No documented stable principal is returned here, so quota_group stays unset.
"""

from __future__ import annotations

import json

from .base import CatalogModel, ProviderError, ProviderSnapshot, QuotaWindow, finite_number, parse_time, request_json

USAGE_URL = "https://nano-gpt.com/api/subscription/v1/usage"
CATALOG_URL = "https://nano-gpt.com/api/v1/models?detailed=true"
COVERAGE_URL = "https://nano-gpt.com/api/subscription/v1/models?detailed=true"


def _number(value: object) -> float | None:
    # Native quotas are JSON numbers, not coercible strings or booleans.
    try:
        return finite_number(value) if type(value) in (int, float) else None
    except OverflowError:
        return None


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _rows(payload: dict) -> list[dict]:
    rows = payload.get("data")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip() for row in rows
    ):
        raise ProviderError("NanoGPT returned invalid model catalog metadata")
    return rows


def parse_snapshot(usage: dict) -> ProviderSnapshot:
    routing = usage.get("routing")
    paid = routing.get("paidSpendPolicyAllowsBalance") if isinstance(routing, dict) else None
    paid = paid if isinstance(paid, bool) else None
    snapshot = ProviderSnapshot(billing_mode="subscription", extra_usage_enabled=paid)
    limits = usage.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    windows_known = True
    # Image quotas must not throttle this text-model catalog. Trial token quotas
    # are separate native units; never reinterpret them as requests or dollars.
    buckets = [(name, limits.get(name)) for name in ("dailyInputTokens", "weeklyInputTokens")]
    if usage.get("usageUnits") == "tokens":
        trial_limits = usage.get("tokenLimits")
        buckets.append(("tokens", trial_limits.get("total") if isinstance(trial_limits, dict) else None))
    for name, raw_limit in buckets:
        if name in {"dailyInputTokens", "weeklyInputTokens"}:
            windows_known = windows_known and name in limits and name in usage
        else:
            windows_known = windows_known and raw_limit is not None and name in usage
        bucket = usage.get(name)
        if raw_limit is None and bucket is None:
            continue  # Explicitly not configured/applicable, never unlimited.
        limit = _number(raw_limit)
        bucket = bucket if isinstance(bucket, dict) else {}
        used = _number(bucket.get("used"))
        ratio = _number(bucket.get("percentUsed"))
        reset_ms = _number(bucket.get("resetAt"))
        reset = parse_time(reset_ms / 1000) if reset_ms is not None else None
        known = (
            limit is not None
            and limit > 0
            and used is not None
            and used >= 0
            and ratio is not None
            and ratio >= 0
            and reset is not None
            and bucket.get("degraded") is not True
        )
        windows_known = windows_known and known
        snapshot.windows.append(
            QuotaWindow(
                name,
                limit_value=limit if limit is not None and limit >= 0 else None,
                used_value=used if known else None,
                used_ratio=min(ratio, 1.0) if known else None,
                resets_at=reset,
            )
        )
    if usage.get("active") is not True or usage.get("state") not in ("active", "grace"):
        snapshot.detail = "NanoGPT native usage does not confirm an active subscription"
    elif paid is not False:
        snapshot.entitlement_status = "blocked" if paid is True else "unverified"
        snapshot.detail = (
            "NanoGPT key permits pay-as-you-go spending; select subscription-only billing"
            if paid is True
            else "NanoGPT key pay-as-you-go restriction is unverified"
        )
    elif not isinstance(routing, dict) or routing.get("recommendedMode") != "subscription":
        snapshot.entitlement_status = (
            "blocked" if isinstance(routing, dict) and routing.get("recommendedMode") == "unavailable" else "unverified"
        )
        snapshot.detail = "NanoGPT native key routing does not permit subscription usage"
    elif not snapshot.windows or not windows_known:
        snapshot.detail = "NanoGPT native subscription token quota observations are unavailable"
    else:
        snapshot.entitlement_status = "verified"
        snapshot.detail = "NanoGPT native subscription quotas verified; key paid-balance spending disabled"
    # allowOverage=false and recommendedMode=subscription alone do not prove the
    # key cannot spend a balance. Only the explicit key policy above does.
    return snapshot


def _catalog(rows: list[dict], covered: set[str], *, eligible: bool) -> list[CatalogModel]:
    models: list[CatalogModel] = []
    seen: set[str] = set()
    for row in rows:
        model = row["id"]
        if model in seen:
            continue
        seen.add(model)
        capabilities = row.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        tools = capabilities.get("tool_calling")
        vision = capabilities.get("vision")
        # Keep only documented model metadata, never arbitrary response fields
        # or locally asserted provider_catalog/subscription_verified evidence.
        metadata = {
            name: row[name]
            for name in (
                "context_length",
                "max_output_tokens",
                "capabilities",
                "supported_service_tiers",
                "architecture",
            )
            if name in row
        }
        models.append(
            CatalogModel(
                model=model,
                display_name=row.get("name") if isinstance(row.get("name"), str) else None,
                context_window=_positive_int(row.get("context_length")),
                max_output_tokens=_positive_int(row.get("max_output_tokens")),
                subscription_eligible=eligible and model in covered,
                supports_tools=tools if isinstance(tools, bool) else None,
                supports_vision=vision if isinstance(vision, bool) else None,
                raw_metadata_json=json.dumps(metadata),
            )
        )
    return models


async def inspect(key: str) -> ProviderSnapshot:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    usage = await request_json("GET", USAGE_URL, headers=headers)
    snapshot = parse_snapshot(usage)
    catalog: list[dict] = []
    covered: set[str] = set()
    failures: list[str] = []
    # Neither endpoint authenticates a plan: even invalid keys can obtain a
    # public model list. Coverage is a separate filter, never catalog presence.
    for url, label in ((CATALOG_URL, "catalog"), (COVERAGE_URL, "subscription coverage")):
        try:
            rows = _rows(await request_json("GET", url, headers=headers))
            if url == CATALOG_URL:
                catalog = rows
            else:
                covered = {row["id"] for row in rows}
        except ProviderError as exc:
            if exc.permanent:
                raise
            suffix = f" (HTTP {exc.status})" if exc.status is not None else ""
            failures.append(f"NanoGPT {label} unavailable{suffix}")
    if failures:
        snapshot.entitlement_status = "blocked" if snapshot.extra_usage_enabled is True else "unverified"
        snapshot.detail += "; " + "; ".join(failures)
    snapshot.models = _catalog(catalog, covered, eligible=snapshot.entitlement_status == "verified")
    if not any(model.subscription_eligible for model in snapshot.models) and snapshot.entitlement_status == "verified":
        snapshot.entitlement_status = "unverified"
        snapshot.detail += "; no catalog models have confirmed subscription coverage"
    return snapshot
