from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, timedelta

import pytest
from sqlalchemy import text, update

from app.core.crypto import TokenEncryptor
from app.db.models import ModelSource, ModelSourceOAuthCredential
from app.db.session import SessionLocal
from app.modules.model_sources import subscription_runtime as runtime
from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import ProviderError, utcnow

pytestmark = pytest.mark.integration


@pytest.fixture
async def credential_source(db_setup, monkeypatch):
    @asynccontextmanager
    async def background():
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(runtime, "get_background_session", background)
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        source = ModelSource(
            id="src_oauth_cas",
            name="synthetic",
            provider="claude",
            auth_type="claude_oauth",
            base_url="https://api.anthropic.com",
            billing_mode="subscription",
            entitlement_verified_at=utcnow(),
            quota_checked_at=utcnow(),
            extra_usage_enabled=False,
            supports_messages=True,
            entitlement_status="verified",
            oauth_credential=ModelSourceOAuthCredential(
                access_token_encrypted=encryptor.encrypt("synthetic-old-access"),
                refresh_token_encrypted=encryptor.encrypt("synthetic-old-refresh"),
                expires_at=utcnow() + timedelta(seconds=30),
                generation=0,
                account_uuid="synthetic-account",
                organization_uuid="synthetic-org",
            ),
        )
        session.add(source)
        await session.commit()
    return source.id


@pytest.mark.asyncio
async def test_simultaneous_refresh_uses_one_provider_rotation(credential_source, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def refresh(token):
        nonlocal calls
        calls += 1
        assert token == "synthetic-old-refresh"
        entered.set()
        await release.wait()
        return claude.TokenPair(access_token="synthetic-new-access", refresh_token="synthetic-rotated", expires_in=3600)

    monkeypatch.setattr(claude, "refresh", refresh)
    first = asyncio.create_task(runtime.fresh_credential(credential_source))
    await entered.wait()
    second = asyncio.create_task(runtime.fresh_credential(credential_source))
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == 1
    assert {source.oauth_credential.generation for source in results} == {1}
    assert all(
        TokenEncryptor().decrypt(source.oauth_credential.access_token_encrypted) == "synthetic-new-access"
        for source in results
    )
    assert all(source.oauth_credential.refresh_claim_owner is None for source in results)


@pytest.mark.asyncio
async def test_new_import_wins_over_inflight_refresh_result(credential_source, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(token):
        entered.set()
        await release.wait()
        return claude.TokenPair(
            access_token="stale-refresh-result", refresh_token="stale-refresh-token", expires_in=3600
        )

    monkeypatch.setattr(claude, "refresh", refresh)
    pending = asyncio.create_task(runtime.fresh_credential(credential_source))
    await entered.wait()
    original = await runtime.load_source(credential_source)
    await runtime.install_credential(
        credential_source,
        claude.TokenPair(access_token="reauthenticated", refresh_token="new-principal", expires_in=3600),
        expected_generation=0,
        expected_configuration=runtime.configuration_fingerprint(original),
    )
    release.set()
    result = await pending
    assert TokenEncryptor().decrypt(result.oauth_credential.access_token_encrypted) == "reauthenticated"
    assert result.oauth_credential.generation == 1
    assert result.entitlement_status == "unverified"


@pytest.mark.asyncio
async def test_old_refresh_failure_cannot_revoke_reauthenticated_generation(credential_source, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(token):
        entered.set()
        await release.wait()
        raise ProviderError("Claude authorization rejected; sign in again", permanent=True)

    monkeypatch.setattr(claude, "refresh", refresh)
    pending = asyncio.create_task(runtime.fresh_credential(credential_source))
    await entered.wait()
    original = await runtime.load_source(credential_source)
    await runtime.install_credential(
        credential_source,
        claude.TokenPair(access_token="reauthenticated", refresh_token="new", expires_in=3600),
        expected_generation=0,
        expected_configuration=runtime.configuration_fingerprint(original),
    )
    release.set()
    with pytest.raises(ProviderError):
        await pending
    current = await runtime.load_source(credential_source)
    assert current.entitlement_status == "unverified"
    assert current.oauth_credential.generation == 1
    assert current.quota_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_refresh_distinguishes_transient_and_revoked(credential_source, monkeypatch, permanent):
    async def refresh(token):
        raise ProviderError("Safe provider failure", permanent=permanent)

    monkeypatch.setattr(claude, "refresh", refresh)
    if permanent:
        with pytest.raises(ProviderError):
            await runtime.fresh_credential(credential_source)
    else:
        result = await runtime.fresh_credential(credential_source)
        assert TokenEncryptor().decrypt(result.oauth_credential.access_token_encrypted) == "synthetic-old-access"
    current = await runtime.load_source(credential_source)
    assert current.entitlement_status == ("reauth_required" if permanent else "verified")
    assert current.oauth_credential.refresh_claim_owner is None
    assert current.oauth_credential.generation == (1 if permanent else 0)


@pytest.mark.asyncio
async def test_import_route_requires_confirmation_and_never_echoes_tokens(async_client):
    created = await async_client.post(
        "/api/model-sources/",
        json={
            "name": "synthetic",
            "provider": "claude",
            "authType": "claude_oauth",
            "baseUrl": "https://api.anthropic.com",
            "supportsMessages": True,
        },
    )
    assert created.status_code == 200
    source_id = created.json()["id"]
    response = await async_client.post(
        f"/api/model-sources/{source_id}/credentials/import",
        json={
            "credentials": {"accessToken": "synthetic-do-not-echo", "expiresAt": 1900000000000},
            "singleWriterConfirmed": False,
        },
    )
    assert response.status_code == 422
    assert "synthetic-do-not-echo" not in response.text


@pytest.mark.asyncio
async def test_old_forward_rejection_does_not_deactivate_new_login(credential_source):
    original = await runtime.load_source(credential_source)
    await runtime.install_credential(
        credential_source,
        claude.TokenPair(access_token="new", refresh_token="new-refresh", expires_in=3600),
        expected_generation=0,
        expected_configuration=runtime.configuration_fingerprint(original),
    )
    await runtime.on_source_rejection(original, 401, {})
    current = await runtime.load_source(credential_source)
    assert current.entitlement_status == "unverified"
    assert current.quota_error is None


@pytest.mark.asyncio
async def test_policy_forbidden_does_not_mislabel_refresh_token_revoked(credential_source):
    original = await runtime.load_source(credential_source)
    await runtime.on_source_rejection(original, 403, {})
    current = await runtime.load_source(credential_source)
    assert current.entitlement_status == "blocked"
    assert "policy" in current.quota_error
    assert current.oauth_credential.refresh_token_encrypted == original.oauth_credential.refresh_token_encrypted


@pytest.mark.asyncio
async def test_rate_limit_sets_real_cooldown_without_rotating_credential(credential_source):
    original = await runtime.load_source(credential_source)
    await runtime.on_source_rejection(original, 429, {"Retry-After": "120"})
    current = await runtime.load_source(credential_source)
    assert 115 <= (current.cooldown_until - utcnow()).total_seconds() <= 120
    assert current.oauth_credential.generation == 0
    assert current.quota_windows == []


@pytest.mark.asyncio
async def test_oauth_routes_use_camel_case_and_actual_browser_session(async_client, monkeypatch):
    from app.modules.model_sources.oauth import FLOW_COOKIE

    async def no_exchange(*args, **kwargs):
        raise AssertionError("cancelled OAuth must never contact provider")

    monkeypatch.setattr(claude, "token_request", no_exchange)
    created = await async_client.post(
        "/api/model-sources/",
        json={
            "name": "synthetic-oauth-ui",
            "provider": "claude",
            "baseUrl": "https://api.anthropic.com",
        },
    )
    assert created.status_code == 200
    source_id = created.json()["id"]
    started = await async_client.post(f"/api/model-sources/{source_id}/oauth/start")
    assert started.status_code == 200
    flow_id = started.json()["flowId"]
    assert "authorizationUrl" in started.json()
    assert "code_verifier" not in started.text
    assert started.headers["cache-control"] == "no-store"
    status = await async_client.get(f"/api/model-sources/{source_id}/oauth/status", params={"flowId": flow_id})
    assert status.status_code == 200
    assert status.json()["status"] == "pending"
    browser_cookie = async_client.cookies.get(FLOW_COOKIE)
    async_client.cookies.clear()
    cross_session = await async_client.get(f"/api/model-sources/{source_id}/oauth/status", params={"flowId": flow_id})
    assert cross_session.status_code == 400
    async_client.cookies.set(FLOW_COOKIE, browser_cookie, path="/api/model-sources")
    cancelled = await async_client.delete(f"/api/model-sources/{source_id}/oauth/flows/{flow_id}")
    assert cancelled.status_code == 204
    status = await async_client.get(f"/api/model-sources/{source_id}/oauth/status", params={"flowId": flow_id})
    assert status.json()["status"] == "cancelled"
    completed = await async_client.post(
        f"/api/model-sources/{source_id}/oauth/complete", json={"flowId": flow_id, "code": "synthetic-code"}
    )
    assert completed.status_code == 400
    assert "synthetic-code" not in completed.text


@pytest.mark.asyncio
async def test_confirmed_nested_import_encrypts_and_preserves_expiry_without_echo(async_client, monkeypatch):
    from app.modules.model_sources import api
    from app.modules.model_sources.service import _to_response

    async def metadata_not_yet_checked(source_id):
        return _to_response(await runtime.load_source(source_id))

    monkeypatch.setattr(api, "refresh_source_quota", metadata_not_yet_checked)
    created = await async_client.post(
        "/api/model-sources/",
        json={
            "name": "synthetic-import-ui",
            "provider": "claude",
            "baseUrl": "https://api.anthropic.com",
        },
    )
    assert created.status_code == 200
    source_id = created.json()["id"]
    response = await async_client.post(
        f"/api/model-sources/{source_id}/credentials/import",
        json={
            "credentials": {
                "claudeAiOauth": {
                    "accessToken": "synthetic-access-secret",
                    "refreshToken": "synthetic-refresh-secret",
                    "expiresAt": 1900000000000,
                    "subscriptionType": "max",
                }
            },
            "singleWriterConfirmed": True,
        },
    )
    assert response.status_code == 200
    assert "synthetic-access-secret" not in response.text
    assert "synthetic-refresh-secret" not in response.text
    assert response.json()["entitlementStatus"] == "unverified"
    source = await runtime.load_source(source_id)
    assert source.oauth_credential.access_token_encrypted != b"synthetic-access-secret"
    assert TokenEncryptor().decrypt(source.oauth_credential.access_token_encrypted) == "synthetic-access-secret"
    assert TokenEncryptor().decrypt(source.oauth_credential.refresh_token_encrypted) == "synthetic-refresh-secret"
    assert int(source.oauth_credential.expires_at.replace(tzinfo=UTC).timestamp()) == 1900000000
    assert source.oauth_credential.subscription_type is None


@pytest.mark.asyncio
async def test_new_unverified_login_is_not_injected_into_previously_selected_source(credential_source):
    selected = await runtime.load_source(credential_source)
    await runtime.install_credential(
        credential_source,
        claude.TokenPair(access_token="new-principal-secret", refresh_token="new-refresh", expires_in=3600),
        expected_generation=0,
        expected_configuration=runtime.configuration_fingerprint(selected),
    )
    with pytest.raises(ProviderError):
        await runtime.upstream_headers(selected, protocol="messages")
    # Header materialization must not copy the new credential into the old
    # already-admitted snapshot after the source became unverified.
    assert TokenEncryptor().decrypt(selected.oauth_credential.access_token_encrypted) == "synthetic-old-access"


@pytest.mark.asyncio
async def test_declared_standard_key_cannot_gain_coding_eligibility(async_client, monkeypatch):
    from app.modules.model_sources.providers.base import CatalogModel, ProviderSnapshot

    async def synthetic_metadata(key):
        assert key == "synthetic-glm-key"
        return ProviderSnapshot(
            billing_mode="coding_plan",
            entitlement_status="verified",
            extra_usage_enabled=False,
            detected_key_type="unknown",
            models=[CatalogModel("native-glm", subscription_eligible=True)],
        )

    monkeypatch.setattr(runtime.zai, "inspect", synthetic_metadata)
    created = await async_client.post(
        "/api/model-sources/",
        json={
            "name": "synthetic-standard",
            "provider": "zai",
            "keyType": "standard",
            "baseUrl": "https://api.z.ai/api/coding/paas/v4",
            "apiKey": "synthetic-glm-key",
        },
    )
    assert created.status_code == 200
    source_id = created.json()["id"]
    refreshed = await async_client.post(f"/api/model-sources/{source_id}/quota")
    assert refreshed.status_code == 200
    payload = refreshed.json()
    assert payload["keyType"] == "standard"
    assert payload["detectedKeyType"] == "unknown"
    assert payload["entitlementStatus"] == "blocked"
    assert "API balance" in payload["entitlementDetail"]
    assert not any(model["subscriptionEligible"] for model in payload["models"])
    assert "synthetic-glm-key" not in refreshed.text


@pytest.mark.asyncio
async def test_oauth_install_survives_metadata_timestamp_changes(credential_source):
    original = await runtime.load_source(credential_source)
    async with SessionLocal() as session:
        # Metadata polling updates this timestamp, and SQLite server defaults
        # encode whole seconds differently from bound Python datetime values.
        await session.execute(
            text(
                "UPDATE model_sources SET updated_at = CURRENT_TIMESTAMP, "
                "quota_checked_at = CURRENT_TIMESTAMP WHERE id = :id"
            ),
            {"id": credential_source},
        )
        await session.commit()
    await runtime.install_credential(
        credential_source,
        claude.TokenPair(access_token="poll-safe", refresh_token="new", expires_in=3600),
        expected_generation=0,
        expected_configuration=runtime.configuration_fingerprint(original),
    )
    current = await runtime.load_source(credential_source)
    assert TokenEncryptor().decrypt(current.oauth_credential.access_token_encrypted) == "poll-safe"


@pytest.mark.asyncio
async def test_oauth_install_refuses_changed_management_configuration(credential_source):
    original = await runtime.load_source(credential_source)
    async with SessionLocal() as session:
        await session.execute(update(ModelSource).where(ModelSource.id == credential_source).values(is_enabled=False))
        await session.commit()
    with pytest.raises(ProviderError, match="configuration changed"):
        await runtime.install_credential(
            credential_source,
            claude.TokenPair(access_token="must-not-install", refresh_token="new", expires_in=3600),
            expected_generation=0,
            expected_configuration=runtime.configuration_fingerprint(original),
        )
    current = await runtime.load_source(credential_source)
    assert current.oauth_credential.generation == 0
    assert current.is_enabled is False
    assert TokenEncryptor().decrypt(current.oauth_credential.access_token_encrypted) == "synthetic-old-access"
