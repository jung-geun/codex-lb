from __future__ import annotations

from pathlib import Path

import pytest
from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrate import run_upgrade

pytestmark = pytest.mark.integration
PARENT = "20260913_000000_add_oidc_provider_flow"
REVISION = "20261018_000000_add_source_subscriptions"


@pytest.mark.asyncio
async def test_subscription_migration_preserves_legacy_credentials_prices_accounts_and_fences(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'subscriptions.db'}"
    await to_thread.run_sync(lambda: run_upgrade(url, PARENT, bootstrap_legacy=False))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO accounts (id,email,plan_type,access_token_encrypted,refresh_token_encrypted,"
                    "id_token_encrypted,last_refresh,status,codex_installation_id) "
                    "VALUES ('account','user@example.test','team',X'0102',X'0304',X'0506',"
                    "CURRENT_TIMESTAMP,'active','installation')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO api_keys (id,name,key_hash,key_prefix,is_active) "
                    "VALUES ('key','old key','hash','prefix',1)"
                )
            )
            for source_id, credential in (("keyless", None), ("keyed", b"encrypted-secret")):
                await conn.execute(
                    text(
                        "INSERT INTO model_sources (id,name,base_url,api_key_encrypted) VALUES (:id,:id,'http://local/v1',:credential)"
                    ),
                    {"id": source_id, "credential": credential},
                )
                await conn.execute(
                    text(
                        "INSERT INTO model_source_models (source_id,model,input_per_1m,raw_metadata_json) "
                        "VALUES (:id,'custom-model',7.25,:metadata)"
                    ),
                    {"id": source_id, "metadata": '{"reasoning":true}'},
                )
            await conn.execute(
                text(
                    "INSERT INTO http_bridge_sessions "
                    "(id,session_key_kind,session_key_value,session_key_hash,api_key_scope,account_id) "
                    "VALUES ('session','thread','value','sessionhash','key','account')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO http_bridge_operations "
                    "(operation_id,session_id,request_fingerprint,state,account_id,model) "
                    "VALUES ('operation','session','fingerprint','unknown','account','gpt-old')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO request_logs (request_id,model,status,account_id,cost_usd) "
                    "VALUES ('old-request','gpt-old','success','account',1.25)"
                )
            )
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,provider,auth_type,billing_mode,entitlement_status,api_key_encrypted "
                            "FROM model_sources ORDER BY id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert [
                (row["id"], row["provider"], row["auth_type"], row["billing_mode"], row["entitlement_status"])
                for row in rows
            ] == [
                ("keyed", "custom", "api_key", "unknown", "unverified"),
                ("keyless", "custom", "none", "unknown", "unverified"),
            ]
            assert rows[0]["api_key_encrypted"] == b"encrypted-secret"
            assert rows[1]["api_key_encrypted"] is None
            assert (
                await conn.execute(text("SELECT billing_policy FROM api_keys WHERE id='key'"))
            ).scalar_one() == "any"
            models = (
                await conn.execute(
                    text(
                        "SELECT input_per_1m,raw_metadata_json,subscription_eligible,subscription_restricted "
                        "FROM model_source_models"
                    )
                )
            ).all()
            assert models == [(7.25, '{"reasoning":true}', 0, 0)] * 2
            assert (
                await conn.execute(
                    text(
                        "SELECT access_token_encrypted,refresh_token_encrypted,plan_type "
                        "FROM accounts WHERE id='account'"
                    )
                )
            ).one() == (b"\x01\x02", b"\x03\x04", "team")
            assert (
                await conn.execute(
                    text("SELECT state,model,account_id FROM http_bridge_operations WHERE operation_id='operation'")
                )
            ).one() == ("unknown", "gpt-old", "account")
            assert (
                await conn.execute(
                    text(
                        "SELECT cost_usd,provider,served_model,cost_kind "
                        "FROM request_logs WHERE request_id='old-request'"
                    )
                )
            ).one() == (1.25, None, None, None)
            assert (await conn.execute(text("SELECT COUNT(*) FROM model_source_quota_windows"))).scalar_one() == 0
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all() == [REVISION]
        from alembic import command

        from app.db.migrate import _build_alembic_config

        config = _build_alembic_config(url)
        await to_thread.run_sync(lambda: command.downgrade(config, PARENT))
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT state FROM http_bridge_operations WHERE operation_id='operation'"))
            ).scalar_one() == "unknown"
            assert (
                await conn.execute(text("SELECT cost_usd FROM request_logs WHERE request_id='old-request'"))
            ).scalar_one() == 1.25
            assert (await conn.execute(text("SELECT COUNT(*) FROM accounts WHERE id='account'"))).scalar_one() == 1
            assert (
                await conn.execute(text("SELECT api_key_encrypted FROM model_sources WHERE id='keyed'"))
            ).scalar_one() == b"encrypted-secret"
    finally:
        await engine.dispose()
