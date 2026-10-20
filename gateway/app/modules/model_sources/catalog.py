from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.openai.model_registry import (
    MODEL_SOURCE_KIND_OPENAI_COMPATIBLE,
    ReasoningLevel,
    UpstreamModel,
)
from app.core.types import JsonValue
from app.core.usage.pricing import SourceModelPrice, calculate_source_cost
from app.core.usage.pricing_catalog import get_source_model_price
from app.core.utils.json_guards import is_json_list, is_json_mapping
from app.db.models import ModelSource, ModelSourceModel
from app.db.session import detach_session_objects, get_background_session
from app.modules.api_keys.service import ApiKeyData
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.selection import SourceProtocol, source_is_eligible

MODEL_SOURCE_KIND_ANTHROPIC_COMPATIBLE = "anthropic_compatible"
DEFAULT_SOURCE_CONTEXT_WINDOW = 128_000

# ``web_search_preview`` is the legacy alias for ``web_search``; request
# validation normalizes it, but accept both here so operator opt-in covers
# payloads regardless of normalization order.
_SEARCH_TOOL_TYPES = frozenset({"web_search", "web_search_preview"})


@dataclass(frozen=True)
class SourceCatalogEntry:
    model: UpstreamModel
    protocols: tuple[SourceProtocol, ...]
    # Unlike the legacy Responses parseability default, these are true limits.
    context_window: int | None
    max_output_tokens: int | None


def source_catalog_entries(
    sources: list[ModelSource],
    api_key: ApiKeyData | None,
    *,
    protocol: SourceProtocol | None = None,
    require_streaming: bool = False,
    allow_exhausted: bool = False,
) -> list[SourceCatalogEntry]:
    """Apply full source policy; discovery may retain exhausted verified models."""
    entries: list[SourceCatalogEntry] = []
    seen: set[tuple[str, SourceProtocol]] = set()
    for source in sorted(sources, key=lambda item: (-item.priority, item.name, item.id)):
        if source.kind == MODEL_SOURCE_KIND_ANTHROPIC_COMPATIBLE:
            candidates: tuple[SourceProtocol, ...] = ("messages",)
        elif source.kind == MODEL_SOURCE_KIND_OPENAI_COMPATIBLE:
            candidates = ("chat_completions", "responses", "messages", "audio_transcriptions", "embeddings")
        else:
            continue
        for source_model in source.models:
            if api_key and api_key.enforced_model and source_model.model != api_key.enforced_model:
                continue
            if require_streaming and not source_model.supports_streaming:
                continue
            # A Responses-only source must not mask a lower-priority Chat or
            # Messages source sharing its id. Resolve ownership per protocol.
            protocols = tuple(candidate for candidate in candidates if (
                (protocol is None or candidate == protocol)
                and (source_model.model, candidate) not in seen
                and source_is_eligible(
                    source, api_key, source_model.model, protocol=candidate, group_sources=sources,
                    allow_exhausted=allow_exhausted,
                )
            ))
            if not protocols:
                continue
            model = _to_upstream_model(source, source_model)
            model.raw["api_types"] = list(protocols)
            entries.append(SourceCatalogEntry(
                model=model,
                protocols=protocols,
                context_window=source_model.context_window,
                max_output_tokens=source_model.max_output_tokens,
            ))
            seen.update((source_model.model, candidate) for candidate in protocols)
    return entries


async def list_source_catalog_entries(
    api_key: ApiKeyData | None,
    *,
    protocol: SourceProtocol | None = None,
    require_streaming: bool = False,
    allow_exhausted: bool = False,
) -> list[SourceCatalogEntry]:
    async with get_background_session() as session:
        sources = list(await ModelSourcesRepository(session).list_sources())
        detach_session_objects(session)
    return source_catalog_entries(
        sources, api_key, protocol=protocol, require_streaming=require_streaming, allow_exhausted=allow_exhausted,
    )


async def list_source_catalog_models(
    api_key: ApiKeyData | None,
    *,
    protocol: SourceProtocol | None = None,
    require_streaming: bool = False,
) -> list[UpstreamModel]:
    models: dict[str, UpstreamModel] = {}
    for entry in await list_source_catalog_entries(
        api_key, protocol=protocol, require_streaming=require_streaming,
    ):
        existing = models.get(entry.model.slug)
        if existing is None:
            models[entry.model.slug] = entry.model
        else:
            advertised = existing.raw["api_types"]
            if is_json_list(advertised):
                advertised.extend(candidate for candidate in entry.protocols if candidate not in advertised)
    return list(models.values())


def source_models_to_upstream_models(sources: list[ModelSource]) -> list[UpstreamModel]:
    models: list[UpstreamModel] = []
    for source in sources:
        if not source.is_enabled:
            continue
        if source.kind not in {MODEL_SOURCE_KIND_OPENAI_COMPATIBLE, MODEL_SOURCE_KIND_ANTHROPIC_COMPATIBLE}:
            continue
        for source_model in source.models:
            if not source_model.is_enabled:
                continue
            models.append(_to_upstream_model(source, source_model))
    return models


def _to_upstream_model(source: ModelSource, source_model: ModelSourceModel) -> UpstreamModel:
    raw = _raw_metadata(source_model)
    # Operator-side request override config is applied server-side at forwarding
    # time (see source_model_request_overrides); it must never reach the
    # client-visible catalog payloads built from UpstreamModel.raw.
    raw.pop("source_request_overrides", None)
    context_window = source_model.context_window or DEFAULT_SOURCE_CONTEXT_WINDOW
    raw.setdefault("visibility", "list")
    raw.setdefault("shell_type", "shell_command")
    raw.setdefault("max_context_window", context_window)
    raw.setdefault("truncation_policy", {"mode": "tokens", "limit": 10_000})
    raw.setdefault("include_skills_usage_instructions", False)
    raw.setdefault("supports_image_detail_original", False)
    raw.setdefault("supports_search_tool", False)
    raw.setdefault("use_responses_lite", False)
    raw.setdefault("experimental_supported_tools", [])
    if source_model.max_output_tokens is not None:
        raw["max_output_tokens"] = source_model.max_output_tokens
    raw["supports_streaming"] = source_model.supports_streaming
    # source_kind/source_id stay on the UpstreamModel fields only: raw is
    # copied into client-visible payloads (codex models "extra"), and internal
    # source identifiers must not leak to proxy clients.
    raw["model_provider"] = "codex-lb"

    input_modalities = ("text", "image") if source_model.supports_vision else ("text",)
    display_name = source_model.display_name or source_model.model
    # The dashboard's single Reasoning switch is the master gate: it is the
    # only reasoning control an operator has in the UI, so a model with it off
    # must not advertise efforts it will never be allowed to use. Keeping the
    # switch authoritative is what lets the Codex catalog, /v1/models and the
    # dashboard checkbox agree; deriving levels regardless would advertise a
    # capability the chat sanitizer then strips.
    reasoning_opted_in = raw.get("supports_reasoning") is True
    reasoning_levels = _reasoning_levels_from_metadata(raw) if reasoning_opted_in else ()
    default_reasoning_level = (
        _default_reasoning_level_from_metadata(raw, reasoning_levels) if reasoning_opted_in else None
    )
    return UpstreamModel(
        slug=source_model.model,
        display_name=display_name,
        description=display_name,
        context_window=context_window,
        input_modalities=input_modalities,
        supported_reasoning_levels=reasoning_levels,
        default_reasoning_level=default_reasoning_level,
        supports_reasoning_summaries=reasoning_opted_in and raw.get("supports_reasoning_summaries") is True,
        support_verbosity=False,
        default_verbosity=None,
        prefer_websockets=False,
        supports_parallel_tool_calls=source_model.supports_tools,
        supported_in_api=True,
        minimal_client_version=None,
        priority=0,
        available_in_plans=frozenset(),
        source_kind=source.kind,
        source_id=source.id,
        raw=raw,
    )


def _reasoning_levels_from_metadata(raw: dict[str, JsonValue]) -> tuple[ReasoningLevel, ...]:
    """Reasoning efforts advertised for a source model.

    Source catalogs have no first-class reasoning schema, so operators declare
    the efforts their backend accepts under ``supported_reasoning_levels`` in
    ``raw_metadata_json``. Both shapes are accepted::

        ["low", "high", "max"]
        [{"effort": "low", "description": "Low reasoning effort"}]

    Efforts are normalized (trimmed and lowercased) and deduplicated.
    Validation is on shape, not on membership of a fixed vocabulary: backends
    disagree on which efforts exist (GLM exposes ``none``, Model Studio
    includes it, others stop at ``low``/``high``/``max``), so an enum here
    would silently drop efforts a provider really accepts. Malformed entries --
    a non-string, a mapping without a string ``effort``, an empty slug -- are
    ignored, keeping the previous no-reasoning default for models that never
    opted in.
    """
    declared = raw.get("supported_reasoning_levels")
    if not is_json_list(declared):
        return ()
    levels: list[ReasoningLevel] = []
    seen: set[str] = set()
    for item in declared:
        if isinstance(item, str):
            effort = item
            description = f"{item.strip().lower()} reasoning effort"
        elif is_json_mapping(item):
            effort_value = item.get("effort")
            if not isinstance(effort_value, str):
                continue
            effort = effort_value
            description_value = item.get("description")
            description = (
                description_value
                if isinstance(description_value, str)
                else f"{effort.strip().lower()} reasoning effort"
            )
        else:
            continue
        effort = effort.strip().lower()
        if not effort or effort in seen:
            continue
        seen.add(effort)
        levels.append(ReasoningLevel(effort=effort, description=description))
    return tuple(levels)


def _default_reasoning_level_from_metadata(
    raw: dict[str, JsonValue],
    levels: tuple[ReasoningLevel, ...],
) -> str | None:
    """Operator-declared default effort, restricted to the advertised levels."""
    declared = raw.get("default_reasoning_level")
    if not isinstance(declared, str):
        return None
    normalized = declared.strip().lower()
    if not any(level.effort == normalized for level in levels):
        return None
    return normalized


def _enabled_source_model(source: ModelSource, model: str) -> ModelSourceModel | None:
    return next(
        (candidate for candidate in source.models if candidate.model == model and candidate.is_enabled),
        None,
    )


def source_model_reasoning_levels(source: ModelSource, model: str) -> tuple[ReasoningLevel, ...]:
    """Reasoning efforts an opted-in source model declared.

    Gated on ``supports_reasoning`` like the catalog derivation, so the
    unsupported-effort restore cannot hand a declared effort to a model whose
    operator left the Reasoning switch off.
    """
    entry = _enabled_source_model(source, model)
    if entry is None:
        return ()
    raw = _raw_metadata(entry)
    if raw.get("supports_reasoning") is not True:
        return ()
    return _reasoning_levels_from_metadata(raw)


def source_model_supports_reasoning(source: ModelSource, model: str) -> bool:
    """Whether the operator turned the model's Reasoning switch on.

    ``"supports_reasoning": true`` in ``raw_metadata_json`` is the single
    opt-in, written by the dashboard's Reasoning checkbox. Declared levels do
    not imply it: they describe *which* efforts an opted-in backend accepts,
    not *whether* reasoning is allowed at all, and the catalog derivation is
    gated on this same flag so the two can never disagree. Everything else is
    treated as non-reasoning, so client-sent reasoning toggles are stripped
    before forwarding on the chat path.
    """
    entry = _enabled_source_model(source, model)
    if entry is None:
        return False
    return _raw_metadata(entry).get("supports_reasoning") is True


def source_model_request_overrides(source: ModelSource, model: str) -> dict[str, JsonValue]:
    """Operator-configured request overrides for a source model.

    Overrides live under ``"source_request_overrides"`` in
    ``raw_metadata_json`` and are applied server-side when forwarding; they are
    stripped from the client-visible catalog metadata (see
    ``_to_upstream_model``).
    """
    entry = next(
        (candidate for candidate in source.models if candidate.model == model and candidate.is_enabled),
        None,
    )
    if entry is None:
        return {}
    value = _raw_metadata(entry).get("source_request_overrides")
    if not is_json_mapping(value):
        return {}
    return dict(value)


def source_model_supported_tool_types(source: ModelSource, model: str) -> frozenset[str]:
    """Non-function Responses tool types the source model declares support for.

    Function tools are always forwarded to OpenAI-compatible sources; hosted
    tool types are dropped unless the model opts in via
    ``"supports_search_tool": true`` (web search) or lists the tool type in
    ``"experimental_supported_tools"`` in ``raw_metadata_json``.
    """
    entry = next(
        (candidate for candidate in source.models if candidate.model == model and candidate.is_enabled),
        None,
    )
    if entry is None:
        return frozenset()
    raw = _raw_metadata(entry)
    supported: set[str] = set()
    if raw.get("supports_search_tool") is True:
        supported |= _SEARCH_TOOL_TYPES
    experimental = raw.get("experimental_supported_tools")
    if is_json_list(experimental):
        supported.update(item for item in experimental if isinstance(item, str))
    return frozenset(supported)


def source_model_cost_usd(
    source: ModelSource,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_creation_5m_input_tokens: int = 0,
    cache_creation_1h_input_tokens: int = 0,
    served_model: str | None = None,
    service_tier: str | None = None,
) -> float | None:
    """Estimate only measured components with known rates; never invent free usage."""
    entry = next(
        (candidate for candidate in source.models if candidate.model == model and candidate.is_enabled),
        None,
    )
    if entry is None:
        return None
    return calculate_source_cost(
        get_source_model_price(source.provider, served_model or entry.upstream_model or model) or SourceModelPrice(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
        service_tier=service_tier,
        overrides={
            name: getattr(entry, name)
            for name in (
                "input_per_1m", "cached_input_per_1m", "output_per_1m",
                "cache_creation_per_1m", "cache_creation_1h_per_1m",
            )
        },
    )


def source_model_audio_cost_usd(source: ModelSource, model: str, audio_seconds: float) -> float | None:
    """Price transcribed audio against the source model's per-minute rate.

    Returns ``None`` when the model has no ``audio_per_minute`` rate so the
    caller can fall back to token pricing (or fail closed for limited keys).
    """
    entry = next(
        (candidate for candidate in source.models if candidate.model == model and candidate.is_enabled),
        None,
    )
    if entry is None or entry.audio_per_minute is None:
        return None
    if audio_seconds <= 0:
        return 0.0
    return (audio_seconds / 60.0) * entry.audio_per_minute


def _raw_metadata(source_model: ModelSourceModel) -> dict[str, JsonValue]:
    if source_model.raw_metadata_json is None:
        return {}
    parsed = json.loads(source_model.raw_metadata_json)
    return parsed if isinstance(parsed, dict) else {}


def source_model_supports_vision(source: ModelSource, model: str) -> bool:
    """Whether the enabled ``ModelSourceModel`` entry for ``model`` declares ``supports_vision`` (#2123 WP-C1)."""

    entry = _enabled_source_model(source, model)
    return entry is not None and bool(entry.supports_vision)
