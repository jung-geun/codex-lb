"""Native OMP discovery projected from the authorized gateway catalogs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Security
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.auth.dependencies import validate_gateway_api_key
from app.core.openai.model_registry import MODEL_SOURCE_KIND_SUBSCRIPTION, get_model_registry
from app.core.types import JsonValue
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.catalog import (
    MODEL_SOURCE_KIND_ANTHROPIC_COMPATIBLE,
    SourceCatalogEntry,
    list_source_catalog_entries,
)
from app.modules.proxy.schemas import CodexModelsResponse

Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


class Thinking(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    mode: Literal["effort", "budget", "google-level", "anthropic-adaptive", "anthropic-budget-effort"]
    efforts: list[Effort] = Field(min_length=1)
    defaultLevel: Effort | None = None
    effortMap: dict[Effort, str] | None = None
    effortBudgets: dict[Effort, int] | None = None
    supportsDisplay: bool | None = None
    prefixBinding: bool | None = None
    suppressWhenOff: bool | None = None
    requiresEffort: bool | None = None
    unverified: bool | None = None


class Compat(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    supportsStore: bool | None = None
    supportsDeveloperRole: bool | None = None
    supportsReasoningEffort: bool | None = None
    supportsUsageInStreaming: bool | None = None
    supportsStrictMode: bool | None = None
    supportsToolChoice: bool | None = None
    supportsParallelToolCalls: bool | None = None
    maxTokensField: Literal["max_tokens", "max_completion_tokens"] | None = None


class RemoteCompaction(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    enabled: bool | None = None
    api: Literal["openai-codex-responses"] | None = None
    endpoint: Literal["/responses/compact", "/responses/compact/stream", "/responses/compact/v2"] | None = None
    v2StreamingEnabled: bool | None = None
    v2Endpoint: Literal["/responses/compact", "/responses/compact/stream", "/responses/compact/v2"] | None = None
    streamingEndpoint: Literal["/responses/compact", "/responses/compact/stream", "/responses/compact/v2"] | None = None


class OmpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    name: str
    api: Literal["openai-codex-responses", "anthropic-messages", "openai-completions"]
    basePath: Literal["/backend-api/codex", "/", "/v1"]
    reasoning: bool
    input: list[Literal["text", "image"]]
    contextWindow: int = Field(gt=0, le=100_000_000)
    maxTokens: int = Field(gt=0, le=100_000_000)
    thinking: Thinking | None = None
    compat: Compat | None = None
    preferWebsockets: bool | None = None
    remoteCompaction: RemoteCompaction | None = None


router = APIRouter(prefix="/v1/omp", tags=["proxy"])


def _positive_limit(value: JsonValue) -> int | None:
    return value if type(value) is int and 0 < value <= 100_000_000 else None


def _thinking(raw: dict[str, JsonValue], efforts: list[str], default: str | None, *, native: bool) -> Thinking | None:
    declared = raw.get("thinking")
    if isinstance(declared, dict) and (not native or "mode" in declared):
        try:
            thinking = Thinking.model_validate(declared)
        except ValidationError:
            return None
        if thinking.defaultLevel is not None and thinking.defaultLevel not in thinking.efforts:
            return None
        if len(set(thinking.efforts)) != len(thinking.efforts):
            return None
        if thinking.effortBudgets and any(_positive_limit(value) is None for value in thinking.effortBudgets.values()):
            return None
        if thinking.effortMap and any(
            not value or len(value) > 40 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in value)
            for value in thinking.effortMap.values()
        ):
            return None
        return thinking
    # Anthropic adaptive and budget modes are not interchangeable. Only provider
    # metadata may identify that mode; a model name must never stand in for it.
    levels = list(dict.fromkeys(effort for effort in efforts if effort in _EFFORTS))
    if not levels:
        return None
    if native:
        capabilities = raw.get("capabilities")
        capability = capabilities.get("thinking") if isinstance(capabilities, dict) else None
        types = capability.get("types") if isinstance(capability, dict) else None
        flags = declared if isinstance(declared, dict) else {}

        def supported(name: str) -> bool:
            if isinstance(flags.get(name), bool):
                return flags[name] is True
            entry = types.get(name) if isinstance(types, dict) else None
            return isinstance(entry, dict) and entry.get("supported") is True

        mode = "anthropic-adaptive" if supported("adaptive") else (
            "anthropic-budget-effort" if supported("enabled") else None
        )
        if mode is None:
            return None
        return Thinking.model_validate({
            "mode": mode, "efforts": levels,
            **({"defaultLevel": default} if default in levels else {}),
        })
    return Thinking.model_validate(
        {
            "mode": "effort",
            "efforts": levels,
            **({"defaultLevel": default} if default in levels else {}),
        }
    )


def _compat(raw: dict[str, JsonValue]) -> Compat | None:
    declared = raw.get("compat")
    if not isinstance(declared, dict):
        return None
    try:
        compat = Compat.model_validate(declared)
    except ValidationError:
        return None
    return compat if compat.model_dump(exclude_none=True) else None


def _source_model(entry: SourceCatalogEntry, *, context_window: int) -> OmpModel | None:
    model = entry.model
    # The legacy Responses catalog synthesizes a context default for parsing.
    # It is not evidence of the upstream's capacity and cannot feed OMP.
    if _positive_limit(entry.context_window) is None or _positive_limit(entry.max_output_tokens) is None:
        return None
    if "chat_completions" in entry.protocols and model.source_kind != MODEL_SOURCE_KIND_ANTHROPIC_COMPATIBLE:
        api, base_path = "openai-completions", "/v1"
    elif "messages" in entry.protocols:
        api, base_path = "anthropic-messages", "/"
    else:
        return None
    reasoning = model.raw.get("supports_reasoning") is True
    return OmpModel(
        id=model.slug,
        name=model.display_name,
        api=api,
        basePath=base_path,
        reasoning=reasoning,
        input=[item for item in model.input_modalities if item in {"text", "image"}],
        contextWindow=context_window,
        maxTokens=entry.max_output_tokens,
        thinking=_thinking(
            model.raw,
            [level.effort for level in model.supported_reasoning_levels],
            model.default_reasoning_level,
            native=api == "anthropic-messages",
        )
        if reasoning
        else None,
        compat=_compat(model.raw),
    )


async def build_omp_models_response(api_key: ApiKeyData | None) -> Response:
    # Import lazily: api.py owns the existing catalog policy, limit reservation,
    # context overrides and Codex wire repair. Do not fork its model SSOT here.
    from app.modules.proxy import api as proxy_api

    response = await proxy_api._build_codex_models_response(api_key)
    if not isinstance(response, JSONResponse) or response.status_code != 200:
        return response
    catalog = CodexModelsResponse.model_validate_json(response.body)
    registry = get_model_registry().get_models_with_fallback()
    public = {item.id: item for item in catalog.data}
    models: list[OmpModel] = []
    seen: set[str] = set()
    for item in catalog.models:
        upstream = registry.get(item.slug)
        if upstream is None or upstream.source_kind != MODEL_SOURCE_KIND_SUBSCRIPTION or item.visibility != "list":
            continue
        # Even if a Codex row lacks usable limits, a colliding source must not
        # take ownership of a GPT id and silently change its native protocol.
        seen.add(item.slug)
        metadata = public.get(item.slug)
        max_tokens = _positive_limit((item.model_extra or {}).get("max_output_tokens"))
        if max_tokens is None and metadata is not None and metadata.metadata is not None:
            max_tokens = _positive_limit(metadata.metadata.max_output_tokens)
        if max_tokens is None or _positive_limit(item.context_window) is None:
            continue
        raw = item.model_extra or {}
        reasoning = bool(item.supported_reasoning_levels)
        compaction = None
        if item.slug.startswith("gpt-"):
            declared = raw.get("remoteCompaction")
            if isinstance(declared, dict):
                try:
                    compaction = RemoteCompaction.model_validate(declared)
                except ValidationError:
                    pass
            else:
                compaction = RemoteCompaction(enabled=True, api="openai-codex-responses", endpoint="/responses/compact")
        models.append(
            OmpModel(
                id=item.slug,
                name=item.display_name,
                api="openai-codex-responses",
                basePath="/backend-api/codex",
                reasoning=reasoning,
                input=[value for value in item.input_modalities if value in {"text", "image"}],
                contextWindow=item.context_window,
                maxTokens=max_tokens,
                thinking=_thinking(
                    raw,
                    [level.effort for level in item.supported_reasoning_levels],
                    item.default_reasoning_level,
                    native=False,
                )
                if reasoning
                else None,
                compat=_compat(raw),
                preferWebsockets=item.prefer_websockets,
                remoteCompaction=compaction,
            )
        )
    overrides = await proxy_api._effective_context_window_overrides()
    # Exhaustion is temporary availability, not removal of a verified model.
    # Dispatch still uses the strict default and cannot consume exhausted quota.
    for entry in await list_source_catalog_entries(api_key, require_streaming=True, allow_exhausted=True):
        if entry.model.slug in seen:
            continue
        model = _source_model(entry, context_window=proxy_api._resolved_context_window(entry.model, overrides))
        if model is not None:
            models.append(model)
            seen.add(model.id)
    return JSONResponse(
        content={"schemaVersion": 1, "models": [model.model_dump(exclude_none=True) for model in models]}
    )


@router.get("/models", response_model=None)
async def omp_models(api_key: ApiKeyData | None = Security(validate_gateway_api_key)) -> Response:
    return await build_omp_models_response(api_key)
