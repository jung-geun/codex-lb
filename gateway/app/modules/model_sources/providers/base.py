from __future__ import annotations

import asyncio
import json as jsonlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import aiohttp

# Metadata endpoints only; never derive these from source/user input. Inference
# origins are validated separately by the source service and forwarding layer.
METADATA_ORIGINS = frozenset({
    "https://api.anthropic.com", "https://platform.claude.com", "https://api.z.ai",
    "https://api.kimi.com", "https://api.moonshot.ai", "https://nano-gpt.com",
    "https://api.deepseek.com", "https://api.openai.com",
})
MAX_METADATA_BYTES = 2 * 1024 * 1024


class ProviderError(ValueError):
    """A deliberately safe error: never carries provider response text or tokens."""

    def __init__(self, message: str, permanent: bool = False, *, status: int | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.status = status


@dataclass(slots=True)
class QuotaWindow:
    window_key: str
    model: str | None = None
    used_ratio: float | None = None
    limit_value: float | None = None
    used_value: float | None = None
    resets_at: datetime | None = None


@dataclass(slots=True)
class CatalogModel:
    model: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    subscription_eligible: bool = False
    line: str | None = None
    raw_metadata_json: str | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    upstream_model: str | None = None


@dataclass(slots=True)
class ProviderSnapshot:
    billing_mode: str = "unknown"
    entitlement_status: str = "unverified"
    detail: str = ""
    extra_usage_enabled: bool | None = None
    quota_group: str | None = None
    windows: list[QuotaWindow] = field(default_factory=list)
    models: list[CatalogModel] = field(default_factory=list)
    account_uuid: str | None = None
    organization_uuid: str | None = None
    account_label: str | None = None
    subscription_type: str | None = None
    detected_key_type: str = "unknown"
    balances: list[dict[str, str | None]] = field(default_factory=list)
    balance_is_available: bool | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_time(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000 if value >= 1e12 else value, timezone.utc).replace(tzinfo=None)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


async def request_json(
    method: str, url: str, *, headers: dict[str, str], json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    if (
        f"{parsed.scheme}://{parsed.netloc}" not in METADATA_ORIGINS
        or parsed.username or parsed.password or parsed.fragment
    ):
        raise ProviderError("Provider metadata origin is not allowed")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), trust_env=False) as client:
            async with client.request(method, url, headers=headers, json=json, allow_redirects=False) as response:
                if response.status < 200 or response.status >= 300:
                    # A profile 403 may mean region/org policy, not revoked OAuth.
                    raise ProviderError(
                        f"Provider metadata request rejected (HTTP {response.status})",
                        permanent=response.status == 401,
                        status=response.status,
                    )
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_METADATA_BYTES:
                        raise ProviderError("Provider metadata response exceeds size limit")
                try:
                    payload = jsonlib.loads(chunks)
                except (ValueError, UnicodeError) as exc:
                    raise ProviderError("Provider returned invalid metadata") from exc
                if not isinstance(payload, dict):
                    raise ProviderError("Provider returned unsupported metadata shape")
                return payload
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ProviderError("Provider metadata temporarily unavailable") from exc
