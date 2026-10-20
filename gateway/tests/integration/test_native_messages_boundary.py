from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import ApiKeyUsageReservation, ModelSourceRequestOperation
from app.db.session import SessionLocal
from app.modules.model_sources.anthropic import continuation_token, native_source_principal
from app.modules.model_sources.native_operations import NativeOperationsRepository
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.selection import selected_model_entry, source_is_eligible
from app.modules.proxy.messages_api import _fingerprint, _legacy_fingerprints, _literal_fingerprint
from tests.integration.model_source_helpers import _enable_api_key_auth

pytestmark = pytest.mark.integration


async def _native_source(client, *, name="native", model="claude-native-boundary") -> str:
    response = await client.post(
        "/api/model-sources/",
        json={
            "name": name,
            "baseUrl": "http://127.0.0.1:1",
            "authType": "none",
            "supportsChatCompletions": False,
            "supportsResponses": False,
            "supportsMessages": True,
            "supportsCountTokens": False,
            "models": [{"model": model, "contextWindow": 200000, "maxOutputTokens": 32000}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _key(client, source_id: str, **policy) -> tuple[str, str]:
    created = await client.post(
        "/api/api-keys/",
        json={
            "name": "native-boundary-key",
            "assignedSourceIds": [source_id],
            **policy,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["key"], created.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("authorization", "Bearer first"), ("x-api-key", "second")],
        [("x-api-key", "first"), ("x-api-key", "first")],
        [("authorization", "Bearer first"), ("authorization", "Bearer first")],
        [("authorization", "Basic first"), ("x-api-key", "first")],
    ],
)
async def test_native_auth_rejects_ambiguous_credentials_with_anthropic_envelope(async_client, headers):
    response = await async_client.post("/v1/messages", headers=headers, json={})
    assert response.status_code == 401
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "authentication_error"
    assert "first" not in response.text and "second" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("dual", [False, True])
async def test_native_api_key_header_authenticates_before_invalid_json(async_client, dual):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    key, _ = await _key(async_client, source_id)
    headers = {"x-api-key": key, "content-type": "application/json"}
    if dual:
        headers["authorization"] = f"Bearer {key}"
    response = await async_client.post("/v1/messages", headers=headers, content=b"{invalid")
    assert response.status_code == 400
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Request body must be valid JSON",
        },
    }


@pytest.mark.asyncio
async def test_count_tokens_unsupported_never_substitutes_an_estimate(async_client):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    key, key_id = await _key(async_client, source_id)
    response = await async_client.post(
        "/v1/messages/count_tokens",
        headers={"x-api-key": key},
        json={
            "model": "claude-native-boundary",
            "messages": [{"role": "user", "content": "count me"}],
        },
    )
    assert response.status_code == 501
    assert response.json()["type"] == "error"
    assert "input_tokens" not in response.json()
    async with SessionLocal() as session:
        reservations = await session.execute(
            select(ApiKeyUsageReservation).where(ApiKeyUsageReservation.api_key_id == key_id)
        )
        assert reservations.scalars().all() == []


@pytest.mark.asyncio
async def test_subscription_only_cannot_dispatch_custom_unknown_billing(async_client):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    key, _ = await _key(async_client, source_id, billingPolicy="subscription_only")
    response = await async_client.post(
        "/v1/messages",
        headers={"x-api-key": key},
        json={
            "model": "claude-native-boundary",
            "messages": [],
            "max_tokens": 10,
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "permission_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/chat/completions", {"model": "claude-native-boundary", "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/responses", {"model": "claude-native-boundary", "input": "hi", "stream": False}),
    ],
)
async def test_native_only_model_never_falls_through_to_codex(async_client, path, payload):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    key, _ = await _key(async_client, source_id)
    response = await async_client.post(path, headers={"authorization": f"Bearer {key}"}, json=payload)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_source_unavailable"


@pytest.mark.asyncio
async def test_persisted_unknown_native_request_is_not_dispatched_again(async_client):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    key, key_id = await _key(async_client, source_id)
    payload = {"model": "claude-native-boundary", "messages": [], "max_tokens": 10}
    fingerprint = _fingerprint(payload)
    async with SessionLocal() as session:
        repository = NativeOperationsRepository(session)
        operation_id = await repository.claim_operation(key_id, fingerprint, source_id, payload["model"])
        assert operation_id is not None
        await repository.finish_operation(operation_id, "unknown")
    response = await async_client.post("/v1/messages", headers={"x-api-key": key}, json=payload)
    assert response.status_code == 400
    assert "uncertain" in response.json()["error"]["message"]
    async with SessionLocal() as session:
        operations = (await session.execute(select(ModelSourceRequestOperation))).scalars().all()
        assert len(operations) == 1
        assert operations[0].status == "unknown"


@pytest.mark.asyncio
async def test_completed_native_operations_do_not_permanently_deduplicate_valid_requests(async_client):
    source_id = await _native_source(async_client)
    async with SessionLocal() as session:
        repository = NativeOperationsRepository(session)
        first = await repository.claim_operation("scope", "same-request", source_id, "claude-native-boundary")
        assert first is not None
        await repository.finish_operation(first, "completed")
        second = await repository.claim_operation("scope", "same-request", source_id, "claude-native-boundary")
        assert second is not None and second != first
        await repository.finish_operation(second, "rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,value,content",
    [
        ("signature", "signed-A", [{"type": "thinking", "thinking": "prior", "signature": "signed-A"}]),
        ("tool", "tool-A", [{"type": "tool_use", "id": "tool-A", "name": "lookup", "input": {}}]),
    ],
)
async def test_native_second_turn_stays_with_durable_owner_despite_higher_priority_peer(
    async_client, kind, value, content
):
    await _enable_api_key_auth(async_client)
    source_a = await _native_source(async_client, name="owner-A")
    source_b = await _native_source(async_client, name="higher-priority-B")
    changed = await async_client.patch(
        f"/api/model-sources/{source_b}", json={"priority": 50, "supportsCountTokens": True}
    )
    assert changed.status_code == 200
    key, scope = await _key(async_client, source_a, assignedSourceIds=[source_a, source_b])
    # The source-A response identity has already been committed by the first
    # turn. No affinity-memory entry is involved in this persisted ownership.
    async with SessionLocal() as session:
        source = await ModelSourcesRepository(session).get_by_id(source_a)
        assert source is not None
        recorded = await NativeOperationsRepository(session).record_continuation_owners(
            scope,
            source_a,
            native_source_principal(source),
            {continuation_token(kind, value)},
        )
        assert recorded
    payload = {
        "model": "claude-native-boundary",
        "messages": [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "continue"},
        ],
    }
    response = await async_client.post("/v1/messages/count_tokens", headers={"x-api-key": key}, json=payload)
    # A does not implement counting. B does, so selecting B would attempt its
    # unreachable transport instead of returning this precise unsupported result.
    assert response.status_code == 501
    paused = await async_client.patch(f"/api/model-sources/{source_a}", json={"isEnabled": False})
    assert paused.status_code == 200
    response = await async_client.post("/v1/messages/count_tokens", headers={"x-api-key": key}, json=payload)
    assert response.status_code == 403
    assert "eligible" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_unknown_signed_history_is_not_assumed_account_neutral(async_client):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    key, _ = await _key(async_client, source_id)
    response = await async_client.post(
        "/v1/messages/count_tokens",
        headers={"x-api-key": key},
        json={
            "model": "claude-native-boundary",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "imported", "signature": "unknown-owner"},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 403
    assert "ownership" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_native_db_lease_expiry_releases_capacity_but_not_uncertain_fingerprint(async_client):
    source_id = await _native_source(async_client)
    patched = await async_client.patch(f"/api/model-sources/{source_id}", json={"maxConcurrency": 1})
    assert patched.status_code == 200
    async with SessionLocal() as session:
        repository = NativeOperationsRepository(session)
        first = await repository.claim_operation("lease-scope", "old-fingerprint", source_id, "claude-native-boundary")
        assert first is not None
        with pytest.raises(ProviderError, match="busy"):
            await repository.claim_operation(
                "lease-scope", "different-fingerprint", source_id, "claude-native-boundary"
            )
        # Simulate a crashed worker's committed, now-expired lease, not an
        # in-memory reset or a manual global clearing of uncertain operations.
        await session.execute(
            update(ModelSourceRequestOperation)
            .where(ModelSourceRequestOperation.id == first)
            .values(
                owner_uuid="previous-process",
                lease_expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        await session.commit()
        second = await repository.claim_operation(
            "lease-scope", "different-fingerprint", source_id, "claude-native-boundary"
        )
        assert second is not None
        assert (
            await repository.claim_operation("lease-scope", "old-fingerprint", source_id, "claude-native-boundary")
            is None
        )
        assert await repository.heartbeat_operation(first) is False
        await repository.finish_operation(first, "completed")
        old = await session.get(ModelSourceRequestOperation, first, populate_existing=True)
        live = await session.get(ModelSourceRequestOperation, second, populate_existing=True)
        assert old is not None and old.status == "unknown"
        assert live is not None and live.status == "dispatching" and live.lease_expires_at > utcnow()
        assert await repository.heartbeat_operation(second) is True
        await repository.finish_operation(second, "completed")
        third = await repository.claim_operation(
            "lease-scope", "another-valid-request", source_id, "claude-native-boundary"
        )
        assert third is not None
        await repository.finish_operation(third, "rejected")


@pytest.mark.parametrize("alias", [
    "claude-native-boundary", "anthropic/claude-native-boundary", "claude/claude-native-boundary",
])
def test_native_uncertainty_normalizes_only_trusted_provider_model_identity(alias):
    payload = {"model": alias, "messages": [], "max_tokens": 10, "stream": True}
    canonical = _fingerprint({**payload, "model": "claude-native-boundary", "stream": False})
    assert _fingerprint(payload) == canonical
    assert _fingerprint({**payload, "model": "zai/claude-native-boundary"}) != canonical
    assert _fingerprint({**payload, "model": "operator-alias"}) != canonical


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_model", [
    "claude-native-boundary", "anthropic/claude-native-boundary", "claude/claude-native-boundary",
])
@pytest.mark.parametrize("status", ["dispatching", "unknown"])
async def test_native_claim_atomically_checks_existing_literal_alias_uncertainty(async_client, legacy_model, status):
    source_id = await _native_source(async_client)
    payload = {"model": "claude-native-boundary", "messages": [], "max_tokens": 10}
    async with SessionLocal() as session:
        session.add(ModelSourceRequestOperation(
            id="legacy-native-request", api_key_scope="legacy-scope", source_id=source_id,
            request_fingerprint=_literal_fingerprint({**payload, "model": legacy_model}),
            model=legacy_model, status=status,
        ))
        await session.commit()
    async with SessionLocal() as session:
        repository = NativeOperationsRepository(session)
        assert await repository.has_uncertain_operation(
            "legacy-scope", _fingerprint(payload), legacy_fingerprints=_legacy_fingerprints(payload),
        )
        assert await repository.claim_operation(
            "legacy-scope", _fingerprint(payload), source_id, payload["model"],
            legacy_fingerprints=_legacy_fingerprints(payload),
        ) is None
        rows = (await session.execute(select(ModelSourceRequestOperation))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_native_model_alias_canonicalization_does_not_expand_literal_model_acl(async_client):
    await _enable_api_key_auth(async_client)
    source_id = await _native_source(async_client)
    async with SessionLocal() as session:
        source = await ModelSourcesRepository(session).get_by_id(source_id)
        assert source is not None
        source.provider, source.auth_type, source.billing_mode = "anthropic", "api_key", "api_metered"
        source.api_key_encrypted = TokenEncryptor().encrypt("synthetic-acl-credential")
        source.models[0].model = "anthropic/claude-native-boundary"
        source.models[0].upstream_model = "claude-native-boundary"
        await session.commit()
        # Both spellings genuinely resolve on an eligible trusted provider;
        # only the literal key ACL, not failed alias lookup, may reject it.
        bare = selected_model_entry(source, "claude-native-boundary")
        alias = selected_model_entry(source, "anthropic/claude-native-boundary")
        assert bare is not None and alias is bare
        assert source_is_eligible(source, None, "claude-native-boundary", protocol="messages")
        assert source_is_eligible(source, None, "anthropic/claude-native-boundary", protocol="messages")
    key, _ = await _key(async_client, source_id, allowedModels=["claude-native-boundary"])
    response = await async_client.post(
        "/v1/messages", headers={"x-api-key": key},
        json={"model": "anthropic/claude-native-boundary", "messages": [], "max_tokens": 10},
    )
    assert response.status_code == 403
    async with SessionLocal() as session:
        rows = (await session.execute(select(ModelSourceRequestOperation))).scalars().all()
        assert rows == []
