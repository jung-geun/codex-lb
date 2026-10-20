from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from anyio import to_thread
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrate import _build_alembic_config, run_upgrade

pytestmark = pytest.mark.integration
PARENT = "20261018_020000_attribute_report_sources"
REVISION = "20261019_000000_add_source_routing_and_warmup"


@pytest.mark.asyncio
async def test_source_controls_migration_backfills_and_retains_permanent_claims(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'source-controls.db'}"
    await to_thread.run_sync(lambda: run_upgrade(url, PARENT, bootstrap_legacy=False))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO model_sources (id,name,base_url,provider,auth_type,billing_mode) "
                    "VALUES ('claude-controls','Claude','https://api.anthropic.com','claude','claude_oauth','subscription')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO model_source_oauth_credentials "
                    "(source_id,access_token_encrypted,refresh_token_encrypted,"
                    "expires_at,account_uuid,organization_uuid) "
                    "VALUES ('claude-controls',X'0102',X'0304','2026-10-20 12:00:00','principal','organization')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO model_source_quota_windows (source_id,window_key,used_ratio,observed_at,resets_at) "
                    "VALUES ('claude-controls','five_hour',0.5,'2026-10-19 12:00:00','2026-10-19 17:00:00')"
                )
            )
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.begin() as connection:
            assert (
                await connection.execute(
                    text("SELECT routing_policy,limit_warmup_enabled FROM model_sources WHERE id='claude-controls'")
                )
            ).one() == ("normal", 0)
            assert (
                await connection.execute(
                    text(
                        "SELECT access_token_encrypted,refresh_token_encrypted,account_uuid,organization_uuid "
                        "FROM model_source_oauth_credentials WHERE source_id='claude-controls'"
                    )
                )
            ).one() == (b"\x01\x02", b"\x03\x04", "principal", "organization")
            assert (
                await connection.execute(
                    text("SELECT used_ratio FROM model_source_quota_windows WHERE source_id='claude-controls'")
                )
            ).scalar_one() == 0.5
            await connection.execute(
                text(
                    "UPDATE model_sources SET routing_policy='burn_first',limit_warmup_enabled=1 "
                    "WHERE id='claude-controls'"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO model_source_limit_warmups "
                    "(id,source_id,principal,window_identity,status,attempted_at) "
                    "VALUES ('claim','claude-controls','principal:organization',"
                    "'idle:2026-10-19','dispatching','2026-10-19 12:00:00')"
                )
            )
            assert (
                await connection.execute(
                    text("SELECT window_key,reset_at,model,completed_at,error_message FROM model_source_limit_warmups")
                )
            ).one() == ("five_hour", None, None, None, None)
            # Recreated sources and aliases cannot buy another claim for the same principal/window.
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO model_source_limit_warmups "
                            "(id,principal,window_identity,status,attempted_at) "
                            "VALUES ('duplicate','principal:organization','idle:2026-10-19',"
                            "'dispatching','2026-10-19 12:01:00')"
                        )
                    )
        async with engine.connect() as connection:
            await connection.execute(text("PRAGMA foreign_keys=ON"))
            await connection.commit()
            async with connection.begin():
                await connection.execute(text("DELETE FROM model_sources WHERE id='claude-controls'"))
                assert (
                    await connection.execute(
                        text(
                            "SELECT source_id,principal,window_identity,status "
                            "FROM model_source_limit_warmups WHERE id='claim'"
                        )
                    )
                ).one() == (None, "principal:organization", "idle:2026-10-19", "dispatching")
        config = _build_alembic_config(url)
        await to_thread.run_sync(lambda: command.downgrade(config, PARENT))
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {column["name"] for column in inspect(sync).get_columns("model_sources")}
            )
            assert "routing_policy" not in columns and "limit_warmup_enabled" not in columns
            assert not await connection.run_sync(lambda sync: inspect(sync).has_table("model_source_limit_warmups"))
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO model_sources (id,name,base_url) VALUES ('after-reupgrade','After','https://example.test/v1')"
                )
            )
            assert (
                await connection.execute(
                    text("SELECT routing_policy,limit_warmup_enabled FROM model_sources WHERE id='after-reupgrade'")
                )
            ).one() == ("normal", 0)
    finally:
        await engine.dispose()
