"""Official API-key catalogs, never subscription entitlement or inference checks.

https://developers.openai.com/api/reference/resources/models/methods/list
https://platform.claude.com/docs/en/api/models/list
"""
from __future__ import annotations

import json
from urllib.parse import quote

from .base import CatalogModel, ProviderError, ProviderSnapshot, request_json

OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _support(capabilities: dict, name: str) -> bool | None:
    value = capabilities.get(name)
    supported = value.get("supported") if isinstance(value, dict) else None
    return supported if isinstance(supported, bool) else None


def parse_catalog(payload: dict, *, provider: str) -> list[CatalogModel]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ProviderError("Provider returned invalid model catalog metadata")
    result: list[CatalogModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("Provider returned invalid model catalog entry")
        native = row.get("id")
        if not isinstance(native, str) or not native.strip() or len(provider + "/" + native) > 255:
            raise ProviderError("Provider returned invalid model catalog ID")
        if native in seen or row.get("lifecycle") == "retired":
            continue
        seen.add(native)
        metadata = {"provider_catalog": True}
        context = output = vision = None
        if provider == "anthropic":
            context = _positive_int(row.get("max_input_tokens"))
            output = _positive_int(row.get("max_tokens"))
            capabilities = row.get("capabilities")
            if isinstance(capabilities, dict):
                metadata["capabilities"] = capabilities
                vision = _support(capabilities, "image_input")
                reasoning = _support(capabilities, "thinking")
                if reasoning is not None:
                    metadata["supports_reasoning"] = reasoning
                effort = capabilities.get("effort")
                if isinstance(effort, dict):
                    metadata["supported_reasoning_levels"] = [
                        name for name in ("low", "medium", "high", "xhigh", "max")
                        if _support(effort, name) is True
                    ]
            for name in ("line", "lifecycle", "max_input_tokens", "max_tokens"):
                if row.get(name) is not None:
                    metadata[name] = row[name]
        # OpenAI's list contains identity/ownership, not token limits, prices,
        # modality or tool support. Do not infer these from model names.
        result.append(CatalogModel(
            model=provider + "/" + native,
            upstream_model=native,
            display_name=row.get("display_name") if isinstance(row.get("display_name"), str) else None,
            context_window=context,
            max_output_tokens=output,
            supports_vision=vision,
            raw_metadata_json=json.dumps(metadata),
        ))
    return result


def _snapshot(provider: str, models: list[CatalogModel]) -> ProviderSnapshot:
    return ProviderSnapshot(
        billing_mode="api_metered",
        entitlement_status="unverified",
        detail=(
            f"{provider.title()} API key authenticated for model discovery only; API usage is metered, "
            "not subscription-covered. Inference access, balance, quota and prices were not checked."
        ),
        models=models,
    )


async def inspect_openai(key: str) -> ProviderSnapshot:
    payload = await request_json(
        "GET", OPENAI_MODELS_URL,
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    return _snapshot("openai", parse_catalog(payload, provider="openai"))


async def inspect_anthropic(key: str) -> ProviderSnapshot:
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Accept": "application/json"}
    models: list[CatalogModel] = []
    cursors: set[str] = set()
    cursor = ""
    for _ in range(20):
        url = ANTHROPIC_MODELS_URL + "?limit=1000"
        if cursor:
            url += "&after_id=" + quote(cursor, safe="")
        payload = await request_json("GET", url, headers=headers)
        models.extend(parse_catalog(payload, provider="anthropic"))
        if payload.get("has_more") is False:
            return _snapshot("anthropic", list({model.model: model for model in models}.values()))
        next_cursor = payload.get("last_id")
        if (
            payload.get("has_more") is not True
            or not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor in cursors
        ):
            raise ProviderError("Provider model catalog pagination is invalid")
        cursors.add(next_cursor)
        cursor = next_cursor
    raise ProviderError("Provider model catalog exceeds pagination limit")
