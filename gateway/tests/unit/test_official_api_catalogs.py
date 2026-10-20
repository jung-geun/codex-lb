from __future__ import annotations

import json

import pytest

from app.modules.model_sources.providers import official_apis
from app.modules.model_sources.providers.base import ProviderError


def test_openai_catalog_namespaces_identity_without_guessing_metadata():
    models = official_apis.parse_catalog({"data": [
        {"id": "gpt-example", "owned_by": "synthetic-org"},
        {"id": "gpt-example"},
    ]}, provider="openai")
    assert len(models) == 1
    model = models[0]
    assert model.model == "openai/gpt-example"
    assert model.upstream_model == "gpt-example"
    assert model.context_window is None
    assert model.max_output_tokens is None
    assert model.supports_tools is None
    assert model.supports_vision is None
    assert model.subscription_eligible is False
    assert json.loads(model.raw_metadata_json) == {"provider_catalog": True}


def test_anthropic_catalog_retains_only_supplied_capabilities_and_limits():
    models = official_apis.parse_catalog({"data": [
        {"id": "claude-example", "display_name": "Example", "max_input_tokens": 170000,
         "max_tokens": 12000, "capabilities": {"image_input": {"supported": True},
         "thinking": {"supported": True}, "effort": {"high": {"supported": True}}}},
        {"id": "claude-unknown"},
        {"id": "claude-retired", "lifecycle": "retired"},
    ]}, provider="anthropic")
    first, unknown = models
    assert first.model == "anthropic/claude-example"
    assert first.upstream_model == "claude-example"
    assert first.context_window == 170000
    assert first.max_output_tokens == 12000
    assert first.supports_vision is True
    assert first.supports_tools is None
    metadata = json.loads(first.raw_metadata_json)
    assert metadata["capabilities"]["image_input"] == {"supported": True}
    assert metadata["supports_reasoning"] is True
    assert metadata["supported_reasoning_levels"] == ["high"]
    assert unknown.context_window is None and unknown.max_output_tokens is None
    assert unknown.supports_vision is None
    assert "supports_reasoning" not in json.loads(unknown.raw_metadata_json)
    assert all(not model.subscription_eligible for model in models)


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": [None]}, {"data": [{"id": ""}]}])
def test_invalid_catalog_is_not_a_successful_empty_key_check(payload):
    with pytest.raises(ProviderError):
        official_apis.parse_catalog(payload, provider="openai")


@pytest.mark.asyncio
async def test_paginated_anthropic_catalog_retains_distinct_api_models_without_subscription(monkeypatch):
    pages = iter([
        {"data": [{"id": "claude-a"}], "has_more": True, "last_id": "cursor/?&"},
        {"data": [{"id": "claude-a"}, {"id": "claude-b"}], "has_more": False},
    ])

    async def request(*args, **kwargs):
        return next(pages)

    monkeypatch.setattr(official_apis, "request_json", request)
    snapshot = await official_apis.inspect_anthropic("synthetic-api-key")
    assert [model.model for model in snapshot.models] == ["anthropic/claude-a", "anthropic/claude-b"]
    assert snapshot.entitlement_status == "unverified"
    assert snapshot.billing_mode == "api_metered"
    assert snapshot.windows == []


@pytest.mark.asyncio
async def test_anthropic_pagination_failure_is_not_partial_success(monkeypatch):
    async def request(*args, **kwargs):
        return {"data": [{"id": "claude-a"}], "has_more": True, "last_id": "same"}

    monkeypatch.setattr(official_apis, "request_json", request)
    with pytest.raises(ProviderError):
        await official_apis.inspect_anthropic("synthetic-api-key")
