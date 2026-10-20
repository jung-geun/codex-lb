from __future__ import annotations

from pathlib import Path

import pytest
from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrate import run_upgrade

pytestmark = pytest.mark.integration
PARENT = "20261019_000000_add_source_routing_and_warmup"
REVISION = "20261020_000000_add_api_fallback_monitoring"


@pytest.mark.asyncio
async def test_api_monitoring_upgrade_preserves_credentials_scope_and_subscription_controls(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'api-monitoring.db'}"
    await to_thread.run_sync(lambda: run_upgrade(url, PARENT, bootstrap_legacy=False))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO model_sources (id,name,base_url,provider,auth_type,billing_mode,api_key_encrypted,"
                "routing_policy,limit_warmup_enabled,is_enabled,quota_group) "
                "VALUES ('api','API','https://api.openai.com/v1','openai','api_key','api_metered',X'010203',"
                "'preserve',0,0,'operator-group')"
            ))
            await connection.execute(text(
                "INSERT INTO model_source_models (source_id,model,upstream_model) "
                "VALUES ('api','openai/native','native')"
            ))
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        # A repeated startup must not reset a later explicit opt-out.
        async with engine.begin() as connection:
            assert (await connection.execute(text(
                "SELECT api_fallback_only,api_key_encrypted,routing_policy,limit_warmup_enabled,is_enabled,quota_group "
                "FROM model_sources WHERE id='api'"
            ))).one() == (1, b"\x01\x02\x03", "preserve", 0, 0, "operator-group")
            assert (await connection.execute(text(
                "SELECT model,upstream_model FROM model_source_models WHERE source_id='api'"
            ))).one() == ("openai/native", "native")
            await connection.execute(text("UPDATE model_sources SET api_fallback_only=0 WHERE id='api'"))
            await connection.execute(text(
                "INSERT INTO model_source_api_rate_limits (source_id,model,token_kind,observed_at,tokens_remaining) "
                "VALUES ('api','native','tokens','2030-01-01 12:00:00',0)"
            ))
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as connection:
            assert (await connection.execute(text(
                "SELECT api_fallback_only FROM model_sources WHERE id='api'"
            ))).scalar_one() == 0
            assert (await connection.execute(text(
                "SELECT tokens_remaining FROM model_source_api_rate_limits"
            ))).scalar_one() == 0
            assert (await connection.execute(text("PRAGMA integrity_check"))).scalar_one() == "ok"
    finally:
        await engine.dispose()
