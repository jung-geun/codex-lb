from __future__ import annotations

import pytest

from app.modules.model_sources.providers import base


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://api.anthropic.com/api/oauth/profile",
    "https://api.anthropic.com.evil.invalid/api/oauth/profile",
    "https://api.anthropic.com:444/api/oauth/profile",
    "https://synthetic:secret@api.anthropic.com/api/oauth/profile",
    "https://127.0.0.1/api/oauth/profile",
])
async def test_metadata_refuses_nonfixed_credential_destinations(url, monkeypatch):
    def client(**kwargs):
        raise AssertionError("disallowed URL reached HTTP client")

    monkeypatch.setattr(base.aiohttp, "ClientSession", client)
    with pytest.raises(base.ProviderError, match="origin"):
        await base.request_json("GET", url, headers={"Authorization": "Bearer synthetic-secret"})


@pytest.mark.asyncio
async def test_redirect_is_not_followed_or_reflected_with_upstream_body(monkeypatch):
    observed = {}

    class Response:
        status = 302

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def text(self):
            raise AssertionError("credential-bearing error body must not be consumed")

    class Client:
        def __init__(self, **kwargs):
            observed["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            observed["request"] = kwargs
            return Response()

    monkeypatch.setattr(base.aiohttp, "ClientSession", Client)
    with pytest.raises(base.ProviderError) as caught:
        await base.request_json("GET", "https://api.anthropic.com/api/oauth/profile",
                                headers={"Authorization": "Bearer synthetic-secret"})
    assert observed["request"]["allow_redirects"] is False
    assert observed["session"]["trust_env"] is False
    assert "synthetic-secret" not in str(caught.value)
    assert caught.value.status == 302
    assert caught.value.permanent is False
