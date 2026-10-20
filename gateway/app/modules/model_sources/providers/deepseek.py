"""Read-only direct DeepSeek API discovery; this is not a subscription plan.

https://api-docs.deepseek.com/ (fixed API origin and bearer authentication)
https://api-docs.deepseek.com/api/get-user-balance (/user/balance)
https://api-docs.deepseek.com/api/list-models (/models)

The balance endpoint reports native currency cash/grant balances, never tokens,
quota windows or reset schedules. No amount is converted into token capacity.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .base import CatalogModel, ProviderError, ProviderSnapshot, request_json

BALANCE_URL = "https://api.deepseek.com/user/balance"
CATALOG_URL = "https://api.deepseek.com/models"


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _amount(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise ProviderError("DeepSeek returned invalid balance amount")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ProviderError("DeepSeek returned invalid balance amount") from exc
    if not amount.is_finite():
        raise ProviderError("DeepSeek returned invalid balance amount")
    return value


def parse_snapshot(balance: dict) -> ProviderSnapshot:
    available = balance.get("is_available")
    if not isinstance(available, bool) or not isinstance(balance.get("balance_infos"), list):
        raise ProviderError("DeepSeek returned invalid balance metadata")
    balances: list[dict[str, str | None]] = []
    for row in balance["balance_infos"]:
        if not isinstance(row, dict) or row.get("currency") not in {"USD", "CNY"}:
            raise ProviderError("DeepSeek returned invalid balance currency")
        balances.append({
            "currency": row["currency"],
            "total_balance": _amount(row.get("total_balance")),
            "granted_balance": _amount(row.get("granted_balance"), optional=True),
            "topped_up_balance": _amount(row.get("topped_up_balance"), optional=True),
        })
    return ProviderSnapshot(
        billing_mode="api_metered",
        balances=balances,
        balance_is_available=available,
        detail=(
            "DeepSeek native API balance is available; direct API usage is metered, not subscription-covered"
            if available
            else "DeepSeek native API balance is insufficient; direct API usage is metered, not subscription-covered"
        ),
    )


def _catalog(payload: dict) -> list[CatalogModel]:
    rows = payload.get("data")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip() for row in rows
    ):
        raise ProviderError("DeepSeek returned invalid model catalog metadata")
    models: list[CatalogModel] = []
    seen: set[str] = set()
    for row in rows:
        model = row["id"]
        if model in seen:
            continue
        seen.add(model)
        modalities = row.get("input_modalities")
        metadata = {
            name: row[name]
            for name in (
                "context_window",
                "max_output_tokens",
                "input_modalities",
                "output_modalities",
                "effort",
                "api_capabilities",
            )
            if name in row
        }
        models.append(
            CatalogModel(
                model=model,
                display_name=row.get("name") if isinstance(row.get("name"), str) else None,
                context_window=_positive_int(row.get("context_window")),
                max_output_tokens=_positive_int(row.get("max_output_tokens")),
                supports_vision=("image" in modalities)
                if isinstance(modalities, list) and all(isinstance(value, str) for value in modalities)
                else None,
                # No documented generic tools flag: never infer one from a model ID.
                raw_metadata_json=json.dumps(metadata),
                subscription_eligible=False,
            )
        )
    return models


async def inspect(key: str) -> ProviderSnapshot:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    balance = await request_json("GET", BALANCE_URL, headers=headers)
    snapshot = parse_snapshot(balance)
    # Insufficient balance must not hide the native catalog or seed defaults.
    try:
        snapshot.models = _catalog(await request_json("GET", CATALOG_URL, headers=headers))
    except ProviderError as exc:
        if exc.permanent:
            raise
        suffix = f" (HTTP {exc.status})" if exc.status is not None else ""
        snapshot.detail += "; DeepSeek catalog unavailable" + suffix
    return snapshot
