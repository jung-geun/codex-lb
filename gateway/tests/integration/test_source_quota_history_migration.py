from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrate import _build_alembic_config, run_upgrade

pytestmark = pytest.mark.integration
PARENT = "20261018_000000_add_source_subscriptions"
REVISION = "20261018_010000_add_source_quota_history"


@pytest.mark.asyncio
async def test_history_migration_seeds_only_real_observations_and_preserves_credential_and_quota(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'quota-history.db'}"
    await to_thread.run_sync(lambda: run_upgrade(url, PARENT, bootstrap_legacy=False))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO model_sources (id,name,base_url,provider,auth_type,billing_mode) "
                    "VALUES ('claude-source','Claude','https://api.anthropic.com','claude','claude_oauth','subscription')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO model_source_oauth_credentials "
                    "(source_id,access_token_encrypted,refresh_token_encrypted,"
                    "expires_at,account_uuid,organization_uuid) "
                    "VALUES ('claude-source',X'0102',X'0304','2026-10-19 12:00:00','principal','organization')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO model_source_quota_windows (source_id,window_key,used_ratio,observed_at,resets_at) "
                    "VALUES ('claude-source','seven_day',1.0,'2026-10-18 12:00:00','2026-10-20 12:00:00')"
                )
            )
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT source_id,window_key,used_ratio,observed_at,resets_at "
                        "FROM model_source_quota_observations"
                    )
                )
            ).all() == [("claude-source", "seven_day", 1.0, "2026-10-18 12:00:00", "2026-10-20 12:00:00")]
            assert (
                await connection.execute(
                    text(
                        "SELECT access_token_encrypted,refresh_token_encrypted,account_uuid,organization_uuid "
                        "FROM model_source_oauth_credentials"
                    )
                )
            ).one() == (b"\x01\x02", b"\x03\x04", "principal", "organization")
            assert (
                await connection.execute(text("SELECT used_ratio FROM model_source_quota_windows"))
            ).scalar_one() == 1.0
        # Dropping only new monitoring history never changes the original observation.
        config = _build_alembic_config(url)
        await to_thread.run_sync(lambda: command.downgrade(config, PARENT))
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT window_key,used_ratio,observed_at FROM model_source_quota_observations")
                )
            ).all() == [("seven_day", 1.0, "2026-10-18 12:00:00")]
    finally:
        await engine.dispose()
