from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.core.auth.dashboard_access import guest_principal
from app.core.auth.dependencies import validate_dashboard_session
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus, ApiKey, ModelSource, ModelSourceOAuthCredential
from app.db.session import SessionLocal
from app.modules.request_logs.repository import RequestLogsRepository

pytestmark = pytest.mark.integration


async def _seed_source_accounts() -> None:
    now = utcnow()
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        session.add(Account(
            id="shared", email="native@example.com", plan_type="plus", status=AccountStatus.ACTIVE,
            access_token_encrypted=encryptor.encrypt("native-access"),
            refresh_token_encrypted=encryptor.encrypt("native-refresh"),
            id_token_encrypted=encryptor.encrypt("native-id"), last_refresh=now,
        ))
        session.add_all([
            ModelSource(id="shared", name="Claude registration", provider="claude", kind="anthropic",
                        base_url="https://example.invalid", billing_mode="subscription"),
            ModelSource(id="named", name="Other subscription", provider="custom", kind="openai_compatible",
                        base_url="https://example.invalid", billing_mode="subscription"),
            ModelSource(id="deleted", name="Deleted private name", provider="claude", kind="anthropic",
                        base_url="https://example.invalid", billing_mode="subscription"),
            ModelSource(id="unused", name="Never requested private name", provider="claude", kind="anthropic",
                        base_url="https://example.invalid"),
            ApiKey(id="key-one", name="First key", key_hash="source-key-one", key_prefix="sk-first"),
            ApiKey(id="key-two", name="Second key", key_hash="source-key-two", key_prefix="sk-second"),
        ])
        await session.flush()
        session.add(ModelSourceOAuthCredential(
            source_id="shared", access_token_encrypted=encryptor.encrypt("private-access-token"),
            refresh_token_encrypted=encryptor.encrypt("private-refresh-token"),
            expires_at=now + timedelta(hours=1), account_label="observed@example.com",
        ))
        await session.commit()
        repo = RequestLogsRepository(session)
        for request_id, account_id, source_id, key_id, model, status, age in [
            ("native", "shared", None, "key-one", "shared-model", "success", 1),
            ("claude-new", None, "shared", "key-one", "shared-model", "success", 2),
            ("claude-old", None, "shared", "key-two", "other-model", "error", 200),
            ("other", None, "named", "key-one", "shared-model", "success", 3),
            ("deleted", None, "deleted", "key-one", "shared-model", "success", 4),
        ]:
            await repo.add_log(
                account_id=account_id, model_source_id=source_id, request_id=request_id, model=model,
                provider="claude" if source_id else None, billing_mode="subscription" if source_id else None,
                api_key_id=key_id, input_tokens=10, output_tokens=5, latency_ms=10, status=status,
                error_code="upstream_error" if status == "error" else None,
                requested_at=now - timedelta(minutes=age),
            )
        await session.execute(delete(ModelSource).where(ModelSource.id == "deleted"))
        await session.commit()


@pytest.mark.asyncio
async def test_request_log_source_catalog_identity_collisions_and_deleted_history(async_client, db_setup):
    await _seed_source_accounts()
    response = await async_client.get("/api/request-logs/options")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["accountIds"]) == {"shared", "source:shared", "source:named", "source:deleted"}
    accounts = {entry["id"]: entry for entry in payload["accounts"]}
    assert accounts["shared"]["label"] == "native@example.com"
    assert accounts["source:shared"] == {
        "id": "source:shared", "label": "observed@example.com", "name": "Claude registration",
        "provider": "claude", "isEmail": True,
    }
    assert accounts["source:named"]["label"] == "Other subscription"
    assert accounts["source:deleted"]["label"] == "source:deleted"
    assert accounts["source:deleted"]["name"] is None
    assert "private-access-token" not in response.text
    assert "Never requested private name" not in response.text

    source = await async_client.get("/api/request-logs", params={"accountId": "source:shared", "limit": 1})
    body = source.json()
    assert source.status_code == 200
    assert body["total"] == 2 and body["hasMore"] is True
    row = body["requests"][0]
    assert row["requestId"] == "claude-new"
    assert row["accountId"] is None
    assert row["accountSelector"] == "source:shared"
    assert row["accountLabel"] == "observed@example.com"
    assert row["accountName"] == "Claude registration"
    assert row["accountLabelIsEmail"] is True
    assert row["planType"] is None
    second = await async_client.get("/api/request-logs", params={"accountId": "source:shared", "limit": 1, "offset": 1})
    assert [row["requestId"] for row in second.json()["requests"]] == ["claude-old"]
    native = await async_client.get("/api/request-logs", params={"accountId": "shared"})
    assert [row["requestId"] for row in native.json()["requests"]] == ["native"]
    deleted = await async_client.get("/api/request-logs", params={"accountId": "source:deleted"})
    assert deleted.json()["requests"][0]["accountLabel"] == "source:deleted"
    union = await async_client.get(
        "/api/request-logs", params=[("accountId", "shared"), ("accountId", "source:shared")]
    )
    assert union.json()["total"] == 3


@pytest.mark.asyncio
async def test_source_accounts_intersect_date_key_model_and_status_filters(async_client, db_setup):
    await _seed_source_accounts()
    filters = {"accountId": "source:shared", "apiKeyId": "key-one", "model": "shared-model", "timeframe": "1h"}
    options = await async_client.get("/api/request-logs/options", params=filters)
    assert options.status_code == 200
    assert options.json()["accountIds"] == ["source:shared"]
    assert options.json()["statuses"] == ["ok"]
    listing = await async_client.get("/api/request-logs", params={**filters, "status": "ok"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert [row["requestId"] for row in listing.json()["requests"]] == ["claude-new"]
    empty = await async_client.get("/api/request-logs", params={**filters, "status": "error"})
    assert empty.json()["total"] == 0


@pytest.mark.asyncio
async def test_source_log_identity_and_search_preserve_guest_privacy(app_instance, async_client, db_setup):
    await _seed_source_accounts()
    app_instance.dependency_overrides[validate_dashboard_session] = guest_principal
    try:
        options = await async_client.get("/api/request-logs/options")
        assert options.status_code == 200
        assert all(entry["label"] == entry["id"] and entry["name"] is None for entry in options.json()["accounts"])
        assert options.json()["apiKeys"] == []
        listing = await async_client.get("/api/request-logs", params={"accountId": "source:shared"})
        assert listing.status_code == 200
        assert listing.json()["total"] == 2
        for row in listing.json()["requests"]:
            assert row["accountLabel"] == "source:shared"
            assert row["accountName"] is None and row["apiKeyName"] is None
            assert row["accountLabelIsEmail"] is False
        for secret in ("observed@example.com", "Claude registration", "native@example.com"):
            searched = await async_client.get("/api/request-logs", params={"search": secret})
            assert searched.json()["total"] == 0
        assert "observed@example.com" not in listing.text + options.text
        assert "Claude registration" not in listing.text + options.text
    finally:
        app_instance.dependency_overrides.pop(validate_dashboard_session, None)
    admin = await async_client.get("/api/request-logs", params={"search": "observed@example.com"})
    assert admin.json()["total"] == 2
