"""Kimi Code observations with fail-closed subscription eligibility.

Official protocol bases: https://www.kimi.com/code/docs/en/ . The official CLI
at commit 9ab1286b8fe4e6bcd116949a27ce5e0ac3389c82 establishes GET /coding/v1/models
(auth/platforms.py: id, display_name, context_length, supports_reasoning,
supports_image_in, supports_video_in) and GET /coding/v1/usages
(ui/shell/usage.py: usage and limits counters), both with Bearer authentication.
These are distinct from the Moonshot pay-as-you-go /v1 API.
https://www.kimi.com/code/docs/en/kimi-code/membership.html explicitly documents
paid Extra Usage fallback, shared with Kimi web, and legacy/new plan windows.
Catalog visibility and quota do not prove active plan/model coverage or that
Extra Usage is disabled. No official API-key contract for those attestations
was found, so inspect never assigns subscription eligibility or a plan label.

Ratio pools and their lagging-counter resolution are adapted from TeamClaude
src/backend-quota.js (KIMI, kimiRatio, kimiCount, kimiResolve), pinned at
88fe9b37862c40fefa459a90cd3dbd2c31db98ce. These are observed wire semantics,
not an official billing-attestation contract.

Portions Copyright (c) 2026 KarpelesLab, MIT License:
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import json
from typing import Any

from .base import CatalogModel, ProviderError, ProviderSnapshot, QuotaWindow, finite_number, parse_time, request_json

USAGE_URL = "https://api.kimi.com/coding/v1/usages"
CATALOG_URL = "https://api.kimi.com/coding/v1/models"
MISSING_EVIDENCE = (
    "Kimi Code subscription billing unverified: missing provider-reported active membership, "
    "plan-specific model coverage, and effective Extra Usage/pay-as-you-go-disabled evidence for this key; "
    "the official catalog and usage APIs do not attest these and no official key-authenticated "
    "verification API was found"
)


def _ratio(key: str, pool: Any) -> QuotaWindow | None:
    if not isinstance(pool, dict):
        return None
    ratio = finite_number(pool.get("used_ratio"))
    if ratio is None or ratio < 0:
        return None
    return QuotaWindow(key, used_ratio=min(ratio, 1.0), resets_at=parse_time(pool.get("reset_time")))


def _counter(key: str, detail: Any) -> QuotaWindow | None:
    if not isinstance(detail, dict):
        return None
    limit = finite_number(detail.get("limit"))
    if limit is None or limit <= 0:
        return None
    used = finite_number(detail.get("used"))
    if used is None or used < 0:
        remaining = finite_number(detail.get("remaining"))
        if remaining is None or not 0 <= remaining <= limit:
            return None
        used = limit - remaining
    return QuotaWindow(
        key,
        used_ratio=min(used / limit, 1.0),
        limit_value=limit,
        used_value=used,
        resets_at=parse_time(detail.get("resetTime")),
    )


def _resolve(
    ratio: QuotaWindow | None, counter: QuotaWindow | None, weekly: QuotaWindow | None, monthly: QuotaWindow | None
) -> QuotaWindow | None:
    # Pinned TeamClaude behavior: replace a stuck zero only when both clocks
    # agree within 2s, weekly counters are reliable, and no monthly ratio exists.
    if (
        ratio is not None
        and ratio.used_ratio == 0
        and monthly is None
        and weekly is not None
        and counter is not None
        and counter.used_value is not None
        and counter.used_value > 0
        and counter.resets_at is not None
        and ratio.resets_at is not None
        and abs((counter.resets_at - ratio.resets_at).total_seconds()) <= 2
    ):
        return counter
    return ratio if ratio is not None else counter


def parse_snapshot(body: dict) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(detail=MISSING_EVIDENCE)
    if "error" in body:
        snapshot.detail += "; Kimi usage API returned an unsuccessful response"
        return snapshot
    pools = body.get("usages")
    has_pools = isinstance(pools, dict)
    pools = pools if has_pools else {}
    monthly = _ratio("monthly", pools.get("limit_month_total"))
    weekly = _counter("seven_day", body.get("usage"))
    session = None
    limits = body.get("limits")
    for row in limits if isinstance(limits, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("window"), dict):
            continue
        window = row["window"]
        duration = finite_number(window.get("duration"))
        unit = window.get("timeUnit")
        multiplier = (
            {"TIME_UNIT_MINUTE": 1, "TIME_UNIT_HOUR": 60, "TIME_UNIT_DAY": 1440}.get(unit)
            if isinstance(unit, str)
            else None
        )
        if duration is not None and multiplier is not None and duration * multiplier == 300:
            session = _counter("five_hour", row.get("detail"))
            break
    for pool_key, window_key, counter in (("limit_5h", "five_hour", session), ("limit_7d", "seven_day", weekly)):
        # An explicitly supplied pool set is authoritative about which windows
        # exist. New plans do not acquire a fabricated weekly legacy window.
        if has_pools and pool_key not in pools:
            continue
        resolved = _resolve(_ratio(window_key, pools.get(pool_key)), counter, weekly, monthly)
        if resolved is not None:
            snapshot.windows.append(resolved)
    if monthly is not None:
        snapshot.windows.append(monthly)
    if not snapshot.windows:
        snapshot.detail += "; Kimi usage API returned no usable quota windows"
    return snapshot


def parse_catalog(body: dict) -> list[CatalogModel]:
    rows = body.get("data")
    if "error" in body or not isinstance(rows, list):
        raise ProviderError("Kimi Code model catalog returned an unsupported response")
    models: list[CatalogModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("id")
        if not isinstance(model, str) or not model.strip() or model in seen:
            continue
        seen.add(model)
        context = row.get("context_length")
        display = row.get("display_name")
        vision = row.get("supports_image_in")
        metadata = {"provider_catalog": True, "catalog_endpoint": CATALOG_URL}
        for field in ("supports_reasoning", "supports_image_in", "supports_video_in"):
            if isinstance(row.get(field), bool):
                metadata[field] = row[field]
        models.append(
            CatalogModel(
                model=model,
                display_name=display if isinstance(display, str) else None,
                context_window=context if type(context) is int and context > 0 else None,
                supports_vision=vision if isinstance(vision, bool) else None,
                raw_metadata_json=json.dumps(metadata),
            )
        )
    return models


async def inspect(key: str) -> ProviderSnapshot:
    # Fixed URLs, no environment/user URL interpolation or client impersonation.
    # Shared request_json disables redirects and bounds timeout/response size.
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        body = await request_json("GET", USAGE_URL, headers=headers)
        snapshot = parse_snapshot(body)
    except ProviderError as exc:
        snapshot = ProviderSnapshot(detail=MISSING_EVIDENCE + "; Kimi usage API: " + str(exc))
    try:
        body = await request_json("GET", CATALOG_URL, headers=headers)
        snapshot.models = parse_catalog(body)
    except ProviderError as exc:
        snapshot.detail += "; Kimi Code model catalog: " + str(exc)
    return snapshot
