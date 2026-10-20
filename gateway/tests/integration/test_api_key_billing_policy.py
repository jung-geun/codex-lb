from __future__ import annotations

import pytest

from app.core.auth.dependencies import validate_required_proxy_api_key_authorization
from app.db.models import ApiKey
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_billing_policy_survives_crud_regeneration_and_cached_auth(async_client):
    created = await async_client.post("/api/api-keys/", json={"name": "plan key", "billingPolicy": "subscription_only"})
    assert created.status_code == 200
    data = created.json()
    key_id, secret = data["id"], data["key"]
    assert data["billingPolicy"] == "subscription_only"
    for _ in range(2):
        authenticated = await validate_required_proxy_api_key_authorization(f"Bearer {secret}")
        assert authenticated.billing_policy == "subscription_only"
    async with SessionLocal() as session:
        row = await session.get(ApiKey, key_id)
        assert row is not None and row.billing_policy == "subscription_only"
    listing = await async_client.get("/api/api-keys/")
    assert next(row for row in listing.json() if row["id"] == key_id)["billingPolicy"] == "subscription_only"
    changed = await async_client.patch(f"/api/api-keys/{key_id}", json={"billingPolicy": "any"})
    assert changed.status_code == 200
    assert changed.json()["billingPolicy"] == "any"
    assert (await validate_required_proxy_api_key_authorization(f"Bearer {secret}")).billing_policy == "any"
    regenerated = await async_client.post(f"/api/api-keys/{key_id}/regenerate")
    assert regenerated.status_code == 200
    assert regenerated.json()["billingPolicy"] == "any"
    assert (await validate_required_proxy_api_key_authorization(f"Bearer {regenerated.json()['key']}")).billing_policy == "any"


@pytest.mark.asyncio
async def test_legacy_key_default_and_invalid_billing_policy(async_client):
    created = await async_client.post("/api/api-keys/", json={"name": "legacy"})
    assert created.status_code == 200
    assert created.json()["billingPolicy"] == "any"
    invalid = await async_client.post("/api/api-keys/", json={"name": "invalid", "billingPolicy": "free"})
    assert invalid.status_code == 422
    invalid_update = await async_client.patch(f"/api/api-keys/{created.json()['id']}", json={"billingPolicy": "free"})
    assert invalid_update.status_code == 422
