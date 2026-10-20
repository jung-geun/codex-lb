"""Persist source routing policy and durable Claude limit warmup claims.

Revision ID: 20261019_000000_add_source_routing_and_warmup
Revises: 20261018_020000_attribute_report_sources
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261019_000000_add_source_routing_and_warmup"
down_revision = "20261018_020000_attribute_report_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_sources") as batch:
        batch.add_column(sa.Column("routing_policy", sa.String(), nullable=False, server_default=sa.text("'normal'")))
        batch.add_column(sa.Column("limit_warmup_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "model_source_limit_warmups",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_id", sa.String(), sa.ForeignKey("model_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("window_key", sa.String(), nullable=False, server_default=sa.text("'five_hour'")),
        sa.Column("window_identity", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reset_at", sa.DateTime(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("principal", "window_identity", name="uq_source_limit_warmups_principal_window"),
    )
    op.create_index(
        "ix_source_limit_warmups_source_attempted", "model_source_limit_warmups", ["source_id", "attempted_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_source_limit_warmups_source_attempted", table_name="model_source_limit_warmups")
    op.drop_table("model_source_limit_warmups")
    with op.batch_alter_table("model_sources") as batch:
        batch.drop_column("limit_warmup_enabled")
        batch.drop_column("routing_policy")
