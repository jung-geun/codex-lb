"""API last-priority routing and provider-observed API rate/balance metadata.

Revision ID: 20261020_000000_add_api_fallback_monitoring
Revises: 20261019_000000_add_source_routing_and_warmup
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261020_000000_add_api_fallback_monitoring"
down_revision = "20261019_000000_add_source_routing_and_warmup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_sources") as batch:
        batch.add_column(sa.Column("api_fallback_only", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("api_balance_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("api_balance_observed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("api_balance_is_available", sa.Boolean(), nullable=True))
    op.create_table(
        "model_source_api_rate_limits",
        sa.Column("source_id", sa.String(), sa.ForeignKey("model_sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model", sa.String(), primary_key=True),
        sa.Column("token_kind", sa.String(), primary_key=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("token_limit", sa.BigInteger(), nullable=True),
        sa.Column("tokens_remaining", sa.BigInteger(), nullable=True),
        sa.Column("tokens_reset_at", sa.DateTime(), nullable=True),
        sa.Column("request_limit", sa.BigInteger(), nullable=True),
        sa.Column("requests_remaining", sa.BigInteger(), nullable=True),
        sa.Column("requests_reset_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("model_source_api_rate_limits")
    with op.batch_alter_table("model_sources") as batch:
        batch.drop_column("api_balance_is_available")
        batch.drop_column("api_balance_observed_at")
        batch.drop_column("api_balance_json")
        batch.drop_column("api_fallback_only")
