from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrate import _build_alembic_config, run_upgrade

pytestmark = pytest.mark.integration
PARENT = "20261018_010000_add_source_quota_history"
REVISION = "20261018_020000_attribute_report_sources"
SENTINEL = "\x1f"
AT = datetime(2026, 10, 18, 9)
BUCKET = int(AT.replace(tzinfo=timezone.utc).timestamp())
SOURCE = SENTINEL + "claude-source"


@pytest.mark.asyncio
async def test_report_migration_preserves_pruned_history_and_recovers_raw_source_identity(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'report-source.db'}"
    await to_thread.run_sync(lambda: run_upgrade(url, PARENT, bootstrap_legacy=False))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO accounts (id,email,plan_type,access_token_encrypted,refresh_token_encrypted,"
                    "id_token_encrypted,last_refresh,status,codex_installation_id) "
                    "VALUES ('account','owner@example.test','pro',X'0102',X'0304',X'0506',"
                    "CURRENT_TIMESTAMP,'active','installation')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO model_sources (id,name,base_url,provider) VALUES (:id,'Claude','https://api.anthropic.com','claude')"
                ),
                {"id": SOURCE},
            )
            await connection.execute(
                text("UPDATE account_usage_rollup_state SET reports_folded_through='2026-10-18 10:00:00' WHERE id=1")
            )
            assert (await connection.execute(text("SELECT COUNT(*) FROM account_usage_rollup_state"))).scalar_one() == 1
            # One old NULL-account group combines retained Claude, retained deleted
            # Codex, and pruned unattributable history. That lost history must survive.
            await connection.execute(
                text(
                    "INSERT INTO request_report_hourly_rollups "
                    "(bucket_epoch,account_id,api_key_id,model,useragent_group,conversation_id,first_requested_at,"
                    "request_count,input_tokens,output_tokens,cost_usd) "
                    "VALUES (:bucket,:account_id,:sentinel,'shared-model',:sentinel,:sentinel,"
                    ":at,:count,:input,:output,:cost)"
                ),
                [
                    {
                        "bucket": BUCKET,
                        "account_id": SENTINEL,
                        "sentinel": SENTINEL,
                        "at": AT,
                        "count": 4,
                        "input": 40,
                        "output": 4,
                        "cost": 2.5,
                    },
                    {
                        "bucket": BUCKET,
                        "account_id": "account",
                        "sentinel": SENTINEL,
                        "at": AT,
                        "count": 3,
                        "input": 100,
                        "output": 10,
                        "cost": 1.75,
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO request_logs (request_id,requested_at,account_id,model_source_id,provider,model,"
                    "status,input_tokens,output_tokens,cost_usd) "
                    "VALUES (:id,:at,:account,:source,:provider,'shared-model','success',:input,:output,:cost)"
                ),
                [
                    {
                        "id": "claude-priced",
                        "at": AT,
                        "account": None,
                        "source": SOURCE,
                        "provider": "claude",
                        "input": 10,
                        "output": 1,
                        "cost": 0.5,
                    },
                    {
                        "id": "claude-unpriced",
                        "at": AT,
                        "account": None,
                        "source": SOURCE,
                        "provider": "claude",
                        "input": 20,
                        "output": 2,
                        "cost": None,
                    },
                    {
                        "id": "deleted-codex-free",
                        "at": AT,
                        "account": None,
                        "source": None,
                        "provider": None,
                        "input": 3,
                        "output": 1,
                        "cost": 0.0,
                    },
                    {
                        "id": "codex-zero",
                        "at": AT,
                        "account": "account",
                        "source": None,
                        "provider": None,
                        "input": 20,
                        "output": 2,
                        "cost": 0.0,
                    },
                    {
                        "id": "codex-priced",
                        "at": AT,
                        "account": "account",
                        "source": None,
                        "provider": None,
                        "input": 40,
                        "output": 4,
                        "cost": 1.25,
                    },
                    {
                        "id": "raw-complement",
                        "at": datetime(2026, 10, 18, 10),
                        "account": None,
                        "source": SOURCE,
                        "provider": "claude",
                        "input": 900,
                        "output": 90,
                        "cost": 9.0,
                    },
                ],
            )
            before = (
                await connection.execute(
                    text(
                        "SELECT SUM(request_count),SUM(input_tokens),SUM(output_tokens),SUM(cost_usd) "
                        "FROM request_report_hourly_rollups"
                    )
                )
            ).one()
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as connection:
            assert (
                (
                    await connection.execute(
                        text(
                            "SELECT SUM(request_count),SUM(input_tokens),SUM(output_tokens),SUM(cost_usd) "
                            "FROM request_report_hourly_rollups"
                        )
                    )
                ).one()
                == before
                == (7, 140, 14, 4.25)
            )
            rows = (
                await connection.execute(
                    text(
                        "SELECT account_id,model_source_id,account_type,request_count,input_tokens,output_tokens,"
                        "cost_usd,unknown_cost_count FROM request_report_hourly_rollups"
                    )
                )
            ).all()
            assert set(rows) == {
                (SENTINEL, SENTINEL + SOURCE, "claude", 2, 30, 3, 0.5, 1),
                (SENTINEL, SENTINEL, "codex", 1, 3, 1, 0.0, 0),
                (SENTINEL, SENTINEL, "unknown", 1, 7, 0, 2.0, 1),
                ("account", SENTINEL, "codex", 3, 100, 10, 1.75, 1),
            }
            assert (
                await connection.execute(
                    text("SELECT reports_folded_through FROM account_usage_rollup_state WHERE id=1")
                )
            ).scalar_one() == "2026-10-18 10:00:00"
            assert (await connection.execute(text("SELECT COUNT(*) FROM request_logs"))).scalar_one() == 6
            assert (
                await connection.execute(
                    text("SELECT access_token_encrypted,refresh_token_encrypted FROM accounts WHERE id='account'")
                )
            ).one() == (b"\x01\x02", b"\x03\x04")
        # Downgrading recombines source groups without dropping pruned measures.
        config = _build_alembic_config(url)
        await to_thread.run_sync(lambda: command.downgrade(config, PARENT))
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT SUM(request_count),SUM(input_tokens),SUM(output_tokens),SUM(cost_usd) "
                        "FROM request_report_hourly_rollups"
                    )
                )
            ).one() == before
        await to_thread.run_sync(lambda: run_upgrade(url, REVISION, bootstrap_legacy=False))
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT SUM(request_count),SUM(input_tokens),SUM(output_tokens),SUM(cost_usd) "
                        "FROM request_report_hourly_rollups"
                    )
                )
            ).one() == before
    finally:
        await engine.dispose()
