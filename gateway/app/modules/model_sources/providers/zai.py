"""GLM metadata observations, not subscription billing attestation.

Official inference bases: https://docs.z.ai/devpack/tool/others.md.
The October 16, 2026 notice at
https://docs.z.ai/devpack/notice/api-key-management.md says Standard keys
consume balance even at coding endpoints; Compatible keys additionally depend
on account history and endpoint. https://docs.z.ai/devpack/teamplan.md documents
optional paid Team overage. Neither a monitor tier nor successful inference
proves the billing classification of this credential.

Monitor wire contract adapted from TeamClaude src/backend-quota.js, pinned
88fe9b37862c40fefa459a90cd3dbd2c31db98ce (ZAI). This is an observed console API,
not a published billing-attestation API. The standard /models endpoint is
reported working in https://github.com/zai-org/feedback/issues/789, including
when inference fails with 1113. It is NOT a Coding Plan coverage catalog.
No documented key-authenticated classification/active-plan/overage contract
was found; do not invent fields or probe guessed console endpoints.

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

from .base import CatalogModel, ProviderError, ProviderSnapshot, QuotaWindow, finite_number, parse_time, request_json

MONITOR_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
CATALOG_URL = "https://api.z.ai/api/paas/v4/models"
MISSING_EVIDENCE = (
    "GLM subscription billing unverified: missing provider-reported classification of this exact API key "
    "(Standard, Individual Coding Plan, Team Plan, or Compatible with applicable account history), "
    "active plan/seat evidence, Coding Plan model coverage, and effective paid-overage-disabled evidence "
    "(including Team on-demand overage). Monitor windows and a standard model catalog do not prove these; "
    "no documented key-authenticated APIs for this evidence were found"
)


def parse_snapshot(body: dict) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(detail=MISSING_EVIDENCE)
    data = body.get("data")
    if body.get("code") != 200 or body.get("success") is False or not isinstance(data, dict):
        snapshot.detail += "; GLM quota monitor returned an unsuccessful or unsupported response"
        return snapshot
    limits = data.get("limits")
    if not isinstance(limits, list):
        snapshot.detail += "; GLM quota monitor limits are unavailable"
        return snapshot
    for row in limits:
        if not isinstance(row, dict) or row.get("type") != "TOKENS_LIMIT":
            continue
        # TIME_LIMIT is an MCP allowance, never a token window or cash balance.
        unit, number = row.get("unit"), row.get("number")
        if type(unit) is not int or type(number) is not int:
            continue
        key = {(3, 5): "five_hour", (6, 1): "seven_day"}.get((unit, number))
        percentage = finite_number(row.get("percentage"))
        if key is None or percentage is None or percentage < 0:
            continue
        reset = finite_number(row.get("nextResetTime"))
        snapshot.windows.append(
            QuotaWindow(
                key,
                used_ratio=min(percentage / 100, 1.0),
                resets_at=parse_time(reset) if reset is not None and reset > 0 else None,
            )
        )
    if not snapshot.windows:
        snapshot.detail += "; GLM quota monitor returned no usable token windows"
    return snapshot


def parse_catalog(body: dict) -> list[CatalogModel]:
    rows = body.get("data")
    if "error" in body or not isinstance(rows, list):
        raise ProviderError("GLM standard model catalog returned an unsupported response")
    models: list[CatalogModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("id")
        if not isinstance(model, str) or not model.strip() or model in seen:
            continue
        seen.add(model)
        # The standard OpenAI catalog does not attest plan coverage or limits.
        models.append(
            CatalogModel(
                model=model,
                raw_metadata_json=json.dumps(
                    {
                        "provider_catalog": True,
                        "catalog_endpoint": CATALOG_URL,
                        "coding_plan_coverage_verified": False,
                    }
                ),
            )
        )
    return models


async def inspect(key: str) -> ProviderSnapshot:
    # request_json enforces the fixed HTTPS origin, bounded reads and no redirects.
    try:
        body = await request_json(
            "GET",
            MONITOR_URL,
            headers={
                "Authorization": key,
                "Accept": "application/json",
                "Accept-Language": "en-US,en",
            },
        )
        snapshot = parse_snapshot(body)
    except ProviderError as exc:
        snapshot = ProviderSnapshot(detail=MISSING_EVIDENCE + "; GLM quota monitor: " + str(exc))
    try:
        body = await request_json(
            "GET",
            CATALOG_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        snapshot.models = parse_catalog(body)
    except ProviderError as exc:
        snapshot.detail += "; GLM standard model catalog: " + str(exc)
    return snapshot
