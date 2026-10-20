from __future__ import annotations

# OAuth/usage contracts adapted from TeamClaude v1.1.25, commit
# 88fe9b37862c40fefa459a90cd3dbd2c31db98ce, src/oauth.js.
# Copyright (c) 2026 KarpelesLab. MIT notice in THIRD_PARTY_LICENSES.md.
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .base import (
    CatalogModel,
    ProviderError,
    ProviderSnapshot,
    QuotaWindow,
    finite_number,
    parse_time,
    request_json,
    utcnow,
)

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-cli/2.1.288 (external, cli)"


class TokenPair(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_in: float = Field(gt=0, le=31536000)


async def token_request(body: dict, *, previous_refresh: str | None = None) -> TokenPair:
    try:
        data = await request_json("POST", TOKEN_URL, headers={"Content-Type": "application/json"}, json=body)
    except ProviderError as exc:
        raise ProviderError(
            "Claude authorization rejected; sign in again"
            if exc.status in {400, 401, 403}
            else "Claude authorization temporarily unavailable",
            permanent=exc.status in {400, 401, 403},
            status=exc.status,
        ) from exc
    # Both native expiry forms are supported; neither missing nor malformed
    # expiry is converted into an invented token lifetime.
    if "expires_at" in data:
        absolute = parse_time(data["expires_at"])
        if absolute is None:
            raise ProviderError("Claude returned an invalid token expiration")
        data["expires_in"] = (absolute - utcnow()).total_seconds()
    try:
        pair = TokenPair.model_validate(data)
    except ValidationError as exc:
        raise ProviderError("Claude returned an invalid token response") from exc
    if not pair.access_token.get_secret_value():
        raise ProviderError("Claude returned an empty access token")
    if pair.refresh_token is None or not pair.refresh_token.get_secret_value():
        pair.refresh_token = SecretStr(previous_refresh) if previous_refresh else None
    return pair


async def refresh(refresh_token: str) -> TokenPair:
    return await token_request(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
        previous_refresh=refresh_token,
    )


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "anthropic-version": "2023-06-01",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def parse_snapshot(profile: dict, usage: dict) -> ProviderSnapshot:
    account = profile.get("account") or {}
    org = profile.get("organization") or {}
    if not isinstance(account, dict) or not isinstance(org, dict):
        raise ProviderError("Claude returned invalid profile metadata")
    account_uuid = account.get("uuid")
    org_uuid = org.get("uuid")
    # Imported subscriptionType is never evidence. Only native account/org
    # identity and membership prove the seat behind this exact credential.
    plan = None
    if account.get("has_claude_max") is True:
        plan = "max"
    elif account.get("has_claude_pro") is True:
        plan = "pro"
    elif org.get("organization_type") in {"team", "claude_team"}:
        plan = "team"
    elif org.get("billing_type") in {"team", "stripe_subscription"} and org.get("seat_tier") in {
        "standard",
        "premium",
        "team",
    }:
        plan = "team"
    status = org.get("subscription_status")
    active = status in {"active", "trialing"} if plan == "team" else status in {None, "active", "trialing"}
    extra = usage.get("extra_usage")
    enabled = extra.get("is_enabled") if isinstance(extra, dict) else None
    enabled = enabled if isinstance(enabled, bool) else None
    # Profile has_extra_usage_enabled means org provisioning, not permission to
    # bill this credential. Native usage.extra_usage.is_enabled is authoritative
    # (TeamClaude normalizeSpend), and unknown remains fail-closed.
    snapshot = ProviderSnapshot(
        billing_mode="subscription",
        extra_usage_enabled=enabled,
        account_uuid=account_uuid if isinstance(account_uuid, str) else None,
        organization_uuid=org_uuid if isinstance(org_uuid, str) else None,
        account_label=account.get("email") if isinstance(account.get("email"), str) else None,
        subscription_type=plan,
    )
    if snapshot.account_uuid and snapshot.organization_uuid:
        snapshot.quota_group = f"claude:{snapshot.organization_uuid}:{snapshot.account_uuid}"
    for key, bucket in usage.items():
        if key != "five_hour" and not key.startswith("seven_day"):
            continue
        if not isinstance(bucket, dict):
            continue
        pct = finite_number(bucket.get("utilization", bucket.get("used_percentage")))
        if pct is None or pct < 0:
            continue
        family = key[len("seven_day_") :] if key.startswith("seven_day_") else None
        snapshot.windows.append(
            QuotaWindow(
                key, model=family, used_ratio=min(pct / 100, 1.0), resets_at=parse_time(bucket.get("resets_at"))
            )
        )
    for limit in usage.get("limits", []) if isinstance(usage.get("limits"), list) else []:
        if not isinstance(limit, dict) or limit.get("group") != "weekly":
            continue
        scope = limit.get("scope") or {}
        model = scope.get("model") or {} if isinstance(scope, dict) else {}
        family = model.get("display_name") if isinstance(model, dict) else None
        pct = finite_number(limit.get("percent"))
        if isinstance(family, str) and family.strip() and pct is not None and pct >= 0:
            key = "seven_day_" + family.strip().lower()
            snapshot.windows = [w for w in snapshot.windows if w.window_key != key]
            snapshot.windows.append(
                QuotaWindow(
                    key,
                    model=family.strip().lower(),
                    used_ratio=min(pct / 100, 1),
                    resets_at=parse_time(limit.get("resets_at")),
                )
            )
    shared = {
        w.window_key for w in snapshot.windows if w.model is None and (w.resets_at is not None or w.used_ratio == 0)
    }
    if not plan or not active or not snapshot.account_uuid or not snapshot.organization_uuid:
        snapshot.detail = "Claude profile does not confirm an active Pro, Max, or Team seat"
    elif enabled is not False:
        snapshot.entitlement_status = "blocked" if enabled else "unverified"
        snapshot.detail = (
            "Disable Claude extra usage and refresh quota"
            if enabled
            else "Claude extra-usage restriction is unverified"
        )
    elif not {"five_hour", "seven_day"}.issubset(shared):
        snapshot.detail = "Claude plan quota windows are unavailable"
    else:
        snapshot.entitlement_status = "verified"
        snapshot.detail = "Claude native profile and plan windows verified; extra usage disabled"
        if org.get("has_extra_usage_enabled") is True:
            snapshot.detail += "; organization has extra usage provisioned but usage endpoint reports it disabled"
    return snapshot


async def catalog(token: str, *, eligible: bool) -> list[CatalogModel]:
    models: list[CatalogModel] = []
    cursor = ""
    seen: set[str] = set()
    for _ in range(20):
        url = "https://api.anthropic.com/v1/models?limit=1000"
        if cursor:
            url += "&after_id=" + quote(cursor, safe="")
        data = await request_json("GET", url, headers=headers(token))
        if not isinstance(data.get("data"), list):
            raise ProviderError("Claude model catalog is unavailable")
        for item in data["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("lifecycle") == "retired":
                continue
            model = item["id"]
            if model in seen:
                continue
            seen.add(model)
            context = item.get("max_input_tokens")
            output = item.get("max_tokens")
            capabilities = item.get("capabilities") or {}
            thinking = capabilities.get("thinking") or {}
            effort = capabilities.get("effort") or {}
            vision = capabilities.get("image_input") or {}
            metadata = {
                "provider_catalog": True,
                "capabilities": capabilities,
                "supports_reasoning": thinking.get("supported") is True,
                "supported_reasoning_levels": [
                    name
                    for name in ("low", "medium", "high", "xhigh", "max")
                    if isinstance(effort.get(name), dict) and effort[name].get("supported") is True
                ],
            }
            types = thinking.get("types") or {}
            metadata["thinking"] = {
                "adaptive": isinstance(types.get("adaptive"), dict) and types["adaptive"].get("supported") is True,
                "enabled": isinstance(types.get("enabled"), dict) and types["enabled"].get("supported") is True,
            }
            models.append(
                CatalogModel(
                    model=model,
                    display_name=item.get("display_name"),
                    context_window=context if isinstance(context, int) and context > 0 else None,
                    max_output_tokens=output if isinstance(output, int) and output > 0 else None,
                    subscription_eligible=eligible,
                    line=item.get("line"),
                    supports_vision=vision.get("supported") is True,
                    raw_metadata_json=json.dumps(metadata),
                )
            )
        if not data.get("has_more"):
            return models
        next_cursor = data.get("last_id")
        if not isinstance(next_cursor, str) or next_cursor == cursor:
            raise ProviderError("Claude model catalog pagination is invalid")
        cursor = next_cursor
    raise ProviderError("Claude model catalog exceeds pagination limit")


async def inspect(token: str) -> ProviderSnapshot:
    profile = await request_json("GET", "https://api.anthropic.com/api/oauth/profile", headers=headers(token))
    usage = await request_json("GET", "https://api.anthropic.com/api/oauth/usage?cedar_ember=1", headers=headers(token))
    snapshot = parse_snapshot(profile, usage)
    # Exhaustion affects dispatch, never catalog discovery/entitlement polling.
    try:
        snapshot.models = await catalog(token, eligible=snapshot.entitlement_status == "verified")
    except ProviderError as exc:
        snapshot.detail += "; " + str(exc)
        # No catalog evidence means no model may acquire eligibility.
    scoped: list[QuotaWindow] = []
    for window in snapshot.windows:
        if window.model is None:
            scoped.append(window)
            continue
        matching = [model for model in snapshot.models if model.line == window.model]
        if not matching:
            # Unknown family metadata is conservative, not silently ignored.
            window.model = None
            scoped.append(window)
        else:
            scoped.extend(
                QuotaWindow(
                    window.window_key + ":" + model.model,
                    model=model.model,
                    used_ratio=window.used_ratio,
                    resets_at=window.resets_at,
                )
                for model in matching
            )
    snapshot.windows = scoped
    return snapshot
