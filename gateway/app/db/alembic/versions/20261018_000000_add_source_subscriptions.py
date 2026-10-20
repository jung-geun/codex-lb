"""Add provider entitlement, actual quota observations, source OAuth and billing policy.

Revision ID: 20261018_000000_add_source_subscriptions
Revises: 20260913_000000_add_oidc_provider_flow
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261018_000000_add_source_subscriptions"
down_revision = "20260913_000000_add_oidc_provider_flow"
branch_labels = None
depends_on = None


def _source_columns() -> list[sa.Column]:
    return [
        sa.Column("provider", sa.String(), nullable=False, server_default="custom"),
        sa.Column("auth_type", sa.String(), nullable=False, server_default="none"),
        sa.Column("billing_mode", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("key_type", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("detected_key_type", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("entitlement_status", sa.String(), nullable=False, server_default="unverified"),
        sa.Column("entitlement_verified_at", sa.DateTime(), nullable=True),
        sa.Column("entitlement_detail", sa.Text(), nullable=True),
        sa.Column("extra_usage_enabled", sa.Boolean(), nullable=True),
        sa.Column("supports_messages", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("supports_count_tokens", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("messages_base_url", sa.String(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quota_group", sa.String(), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
        sa.Column("quota_checked_at", sa.DateTime(), nullable=True),
        sa.Column("quota_error", sa.Text(), nullable=True),
    ]


def _model_columns() -> list[sa.Column]:
    return [
        sa.Column("upstream_model", sa.String(), nullable=True),
        sa.Column("subscription_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subscription_restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cache_creation_per_1m", sa.Float(), nullable=True),
        sa.Column("cache_creation_1h_per_1m", sa.Float(), nullable=True),
    ]


def _log_columns() -> list[sa.Column]:
    return [
        sa.Column(name, type_, nullable=True)
        for name, type_ in (
            ("provider", sa.String()),
            ("served_model", sa.String()),
            ("billing_mode", sa.String()),
            ("cache_creation_input_tokens", sa.Integer()),
            ("cache_creation_5m_input_tokens", sa.Integer()),
            ("cache_creation_1h_input_tokens", sa.Integer()),
            ("cost_kind", sa.String()),
        )
    ]


def upgrade() -> None:
    # ADD COLUMN preserves existing source/model/account/log data; never reinterpret
    # an existing custom endpoint as a verified subscription or rewrite its price.
    for column in _source_columns():
        op.add_column("model_sources", column)
    op.create_index("ix_model_sources_quota_group", "model_sources", ["quota_group"])
    op.execute(sa.text("UPDATE model_sources SET auth_type = 'api_key' WHERE api_key_encrypted IS NOT NULL"))
    for column in _model_columns():
        op.add_column("model_source_models", column)
    op.add_column("api_keys", sa.Column("billing_policy", sa.String(), nullable=False, server_default="any"))
    for column in _log_columns():
        op.add_column("request_logs", column)
    op.create_table(
        "model_source_oauth_credentials",
        sa.Column("source_id", sa.String(), sa.ForeignKey("model_sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("account_uuid", sa.String(), nullable=True),
        sa.Column("organization_uuid", sa.String(), nullable=True),
        sa.Column("account_label", sa.String(), nullable=True),
        sa.Column("subscription_type", sa.String(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refresh_claim_owner", sa.String(), nullable=True),
        sa.Column("refresh_claim_expires_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "model_source_quota_windows",
        sa.Column("source_id", sa.String(), sa.ForeignKey("model_sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("window_key", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("used_ratio", sa.Float(), nullable=True),
        sa.Column("limit_value", sa.Float(), nullable=True),
        sa.Column("used_value", sa.Float(), nullable=True),
        sa.Column("resets_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("used_ratio IS NULL OR (used_ratio >= 0 AND used_ratio <= 1)", name="ck_source_quota_ratio"),
    )
    op.create_table(
        "model_source_request_operations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("api_key_scope", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("owner_uuid", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('dispatching','completed','rejected','unknown')", name="ck_source_operation_status"
        ),
    )
    op.create_index(
        "ix_model_source_request_operations_request_fingerprint",
        "model_source_request_operations",
        ["request_fingerprint"],
    )
    op.create_index(
        "ix_model_source_operations_source_status", "model_source_request_operations", ["source_id", "status"]
    )
    op.create_index(
        "uq_source_operation_active",
        "model_source_request_operations",
        ["api_key_scope", "request_fingerprint"],
        unique=True,
        sqlite_where=sa.text("status IN ('dispatching','unknown')"),
        postgresql_where=sa.text("status IN ('dispatching','unknown')"),
    )
    op.create_table(
        "model_source_continuation_owners",
        sa.Column("api_key_scope", sa.String(), primary_key=True),
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("model_source_continuation_owners")
    op.drop_table("model_source_request_operations")
    op.drop_table("model_source_quota_windows")
    op.drop_table("model_source_oauth_credentials")
    for column in reversed(_log_columns()):
        op.drop_column("request_logs", column.name)
    op.drop_column("api_keys", "billing_policy")
    for column in reversed(_model_columns()):
        op.drop_column("model_source_models", column.name)
    op.drop_index("ix_model_sources_quota_group", table_name="model_sources")
    for column in reversed(_source_columns()):
        op.drop_column("model_sources", column.name)
