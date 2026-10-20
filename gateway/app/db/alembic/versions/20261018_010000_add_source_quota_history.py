"""Retain actual source quota observations without reconstructing prior usage.

Revision ID: 20261018_010000_add_source_quota_history
Revises: 20261018_000000_add_source_subscriptions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261018_010000_add_source_quota_history"
down_revision = "20261018_000000_add_source_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "model_source_quota_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(), sa.ForeignKey("model_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("window_key", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("used_ratio", sa.Float(), nullable=True),
        sa.Column("limit_value", sa.Float(), nullable=True),
        sa.Column("used_value", sa.Float(), nullable=True),
        sa.Column("resets_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "used_ratio IS NULL OR (used_ratio >= 0 AND used_ratio <= 1)",
            name="ck_source_quota_observation_ratio",
        ),
    )
    op.create_index(
        "ix_source_quota_observations_source_time", "model_source_quota_observations", ["source_id", "observed_at"]
    )
    # Seed only the current provider observation at its original timestamp.
    # No sample before that time is knowable and none is fabricated.
    columns = [
        "source_id",
        "window_key",
        "model",
        "used_ratio",
        "limit_value",
        "used_value",
        "resets_at",
        "observed_at",
    ]
    current = sa.table("model_source_quota_windows", *(sa.column(name) for name in columns))
    op.execute(table.insert().from_select(columns, sa.select(*(current.c[name] for name in columns))))


def downgrade() -> None:
    op.drop_index("ix_source_quota_observations_source_time", table_name="model_source_quota_observations")
    op.drop_table("model_source_quota_observations")
