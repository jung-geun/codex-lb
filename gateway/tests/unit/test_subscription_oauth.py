from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from app.modules.model_sources.oauth import OAuthFlows, parse_code
from app.modules.model_sources.provider_schemas import CredentialImportRequest
from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import ProviderError, utcnow


def start(manager: OAuthFlows, binding: str = "browser-a"):
    return manager.start("src_a", binding, generation=None, configuration="synthetic-configuration")


def test_pkce_link_bound_to_real_provider_and_unique_attempt():
    manager = OAuthFlows()
    first = start(manager)
    second = start(manager)
    query = parse_qs(urlsplit(second.authorization_url).query)
    assert second.flow_id != first.flow_id
    assert query["client_id"] == [claude.CLIENT_ID]
    assert query["redirect_uri"] == [claude.REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert manager.status("src_a", first.flow_id, "browser-a").status == "cancelled"
    with pytest.raises(ProviderError, match="dashboard session"):
        manager.status("src_a", second.flow_id, "browser-b")
    with pytest.raises(ProviderError):
        manager.status("src_b", second.flow_id, "browser-a")


@pytest.mark.asyncio
async def test_cancelled_and_expired_codes_never_exchange(monkeypatch):
    calls = []

    async def exchange(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not exchange a cancelled attempt")

    monkeypatch.setattr(claude, "token_request", exchange)
    manager = OAuthFlows()
    cancelled = start(manager)
    manager.cancel("src_a", cancelled.flow_id, "browser-a")
    with pytest.raises(ProviderError, match="already been used"):
        await manager.exchange("src_a", cancelled.flow_id, "browser-a", "code")
    expired = start(manager)
    manager.get("src_a", expired.flow_id, "browser-a").expires_at = utcnow() - timedelta(seconds=1)
    assert manager.status("src_a", expired.flow_id, "browser-a").status == "expired"
    with pytest.raises(ProviderError):
        await manager.exchange("src_a", expired.flow_id, "browser-a", "code")
    assert calls == []


@pytest.mark.asyncio
async def test_code_single_use_and_pkce_exchange_contract(monkeypatch):
    calls = []

    async def exchange(body):
        calls.append(body)
        return claude.TokenPair(access_token="synthetic-access", refresh_token="synthetic-refresh", expires_in=3600)

    monkeypatch.setattr(claude, "token_request", exchange)
    manager = OAuthFlows()
    link = start(manager)
    state = parse_qs(urlsplit(link.authorization_url).query)["state"][0]
    flow, tokens = await manager.exchange("src_a", link.flow_id, "browser-a", "code#" + state)
    assert calls[0]["redirect_uri"] == claude.REDIRECT_URI
    assert calls[0]["code"] == "code"
    assert calls[0]["state"] == state
    assert calls[0]["code_verifier"]
    assert flow.verifier == ""
    assert "synthetic-access" not in repr(tokens)
    with pytest.raises(ProviderError):
        await manager.exchange("src_a", link.flow_id, "browser-a", "code#" + state)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_state_mismatch_consumes_attempt_without_credential_request(monkeypatch):
    async def exchange(body):
        raise AssertionError("mismatched state must never leave gateway")

    monkeypatch.setattr(claude, "token_request", exchange)
    manager = OAuthFlows()
    link = start(manager)
    with pytest.raises(ProviderError, match="state mismatch"):
        await manager.exchange("src_a", link.flow_id, "browser-a", "code#other")
    assert manager.status("src_a", link.flow_id, "browser-a").status == "failed"
    assert "code" not in (manager.status("src_a", link.flow_id, "browser-a").error or "")


@pytest.mark.parametrize("value", [False, None])
def test_import_requires_explicit_single_writer_confirmation(value):
    with pytest.raises(ValidationError):
        CredentialImportRequest.model_validate({"credentials": {"accessToken": "synthetic", "expiresAt": 1900000000000},
                                                "singleWriterConfirmed": value})


def test_parse_callback_checks_state_and_never_accepts_authorize_link():
    assert parse_code(claude.REDIRECT_URI + "?code=synthetic&state=bound", "bound") == "synthetic"
    with pytest.raises(ProviderError):
        parse_code(claude.AUTHORIZE_URL + "?code=true&state=bound", "bound")
    with pytest.raises(ProviderError):
        parse_code(claude.REDIRECT_URI + "?code=synthetic", "bound")
