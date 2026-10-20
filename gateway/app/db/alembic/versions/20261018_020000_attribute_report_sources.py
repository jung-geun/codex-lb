"""Preserve permanent report history while adding source/type attribution.

Revision ID: 20261018_020000_attribute_report_sources
Revises: 20261018_010000_add_source_quota_history
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20261018_020000_attribute_report_sources"
down_revision = "20261018_010000_add_source_quota_history"
branch_labels = None
depends_on = None

TABLE = "request_report_hourly_rollups"
SENTINEL = "\x1f"
LEGACY_KEYS = ("bucket_epoch", "account_id", "api_key_id", "model", "useragent_group", "conversation_id")
KEYS = ("bucket_epoch", "account_id", "model_source_id", "account_type", *LEGACY_KEYS[2:])
MEASURES = (
    "request_count",
    "error_count",
    "cancelled_count",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "reasoning_usage_known_requests",
    "cached_input_tokens",
    "cost_usd",
)


def _create(name: str, *, typed: bool, temporary: bool = False) -> sa.Table:
    keys = KEYS if typed else LEGACY_KEYS
    columns = []
    for key in keys:
        options = {}
        if key == "model_source_id":
            options["server_default"] = sa.text("'\x1f'")
        elif key == "account_type":
            options["server_default"] = sa.text("'codex'")
        columns.append(
            sa.Column(key, sa.BigInteger() if key == "bucket_epoch" else sa.String(), nullable=False, **options)
        )
    columns.append(sa.Column("first_requested_at", sa.DateTime(), nullable=False))
    columns.extend(
        sa.Column(
            name, sa.Float() if name == "cost_usd" else sa.BigInteger(), nullable=False, server_default=sa.text("0")
        )
        for name in (*MEASURES, *(("unknown_cost_count",) if typed else ()))
    )
    return op.create_table(name, *columns, sa.PrimaryKeyConstraint(*keys), prefixes=["TEMPORARY"] if temporary else [])


def _dimension(column):
    # Frozen mirror of the report fold's injective NULL-sentinel encoding.
    return sa.case(
        (column.is_(None), SENTINEL),
        (sa.func.substr(column, 1, 1) == SENTINEL, SENTINEL + column),
        else_=column,
    )


def _retained_groups(bind: Connection, old: sa.Table, watermark) -> sa.Table:
    raw = sa.Table("request_logs", sa.MetaData(), autoload_with=bind)
    if bind.dialect.name == "postgresql":
        bucket = sa.cast(sa.func.floor(sa.extract("epoch", raw.c.requested_at) / 3600) * 3600, sa.BigInteger)
    else:
        bucket = sa.cast(sa.cast(sa.func.strftime("%s", raw.c.requested_at), sa.Integer) / 3600, sa.Integer) * 3600
    whitespace = " \t\n\v\f\r"
    conversation = sa.func.nullif(sa.func.ltrim(sa.func.rtrim(raw.c.conversation_id, whitespace), whitespace), "")
    dimensions = (
        bucket,
        _dimension(raw.c.account_id),
        _dimension(raw.c.model_source_id),
        sa.case(
            (raw.c.model_source_id.is_(None), "codex"),
            else_=sa.func.coalesce(sa.func.nullif(raw.c.provider, ""), "other"),
        ),
        _dimension(raw.c.api_key_id),
        raw.c.model,
        _dimension(raw.c.useragent_group),
        _dimension(conversation),
    )
    named = {name: expression for name, expression in zip(KEYS, dimensions, strict=True)}
    normal = sa.and_(
        sa.or_(raw.c.source.is_(None), raw.c.source != "limit_warmup"),
        sa.or_(raw.c.request_kind.is_(None), raw.c.request_kind.not_in(("warmup", "limit_warmup"))),
    )
    measures = (
        sa.func.count(),
        sa.func.sum(sa.case((raw.c.status.not_in(("success", "cancelled")), 1), else_=0)),
        sa.func.sum(sa.case((raw.c.status == "cancelled", 1), else_=0)),
        sa.func.coalesce(sa.func.sum(raw.c.input_tokens), 0),
        sa.func.sum(sa.func.coalesce(raw.c.output_tokens, raw.c.reasoning_tokens, 0)),
        sa.func.coalesce(sa.func.sum(raw.c.reasoning_tokens), 0),
        sa.func.count(raw.c.reasoning_tokens),
        sa.func.coalesce(sa.func.sum(raw.c.cached_input_tokens), 0),
        sa.func.coalesce(sa.func.sum(raw.c.cost_usd), 0.0),
        sa.func.sum(sa.case((raw.c.cost_usd.is_(None), 1), else_=0)),
    )
    recovered = _create("_report_source_recovery", typed=True, temporary=True)
    # Only subtract raw measures that demonstrably belong to an existing fold.
    # Raw complement at/after the watermark stays raw and is never copied here.
    query = (
        sa.select(*dimensions, sa.func.min(raw.c.requested_at), *measures)
        .select_from(raw.join(old, sa.and_(*(old.c[name] == named[name] for name in LEGACY_KEYS))))
        .where(raw.c.requested_at < watermark, normal)
        .group_by(*dimensions)
    )
    bind.execute(recovered.insert().from_select([*KEYS, "first_requested_at", *MEASURES, "unknown_cost_count"], query))
    return recovered


def _validate_recovery(bind: Connection, old: sa.Table, totals) -> None:
    join = old.outerjoin(totals, sa.and_(*(old.c[name] == totals.c[name] for name in LEGACY_KEYS)))
    invalid = []
    for name in MEASURES:
        delta = old.c[name] - sa.func.coalesce(totals.c[name], 0)
        tolerance = 1e-6 + sa.func.abs(old.c[name]) * 1e-9 if name == "cost_usd" else 0
        invalid.append(delta < -tolerance)
        # A fully retained group must not leave hidden measures without requests.
        invalid.append(
            sa.and_(
                old.c.request_count == sa.func.coalesce(totals.c.request_count, 0),
                sa.func.abs(delta) > tolerance,
            )
        )
    if bind.execute(sa.select(sa.literal(1)).select_from(join).where(sa.or_(*invalid)).limit(1)).first():
        raise RuntimeError("Report history disagrees with retained raw measures; refusing destructive attribution")


def upgrade() -> None:
    bind = op.get_bind()
    old = sa.Table(TABLE, sa.MetaData(), autoload_with=bind)
    state = sa.table("account_usage_rollup_state", sa.column("id"), sa.column("reports_folded_through", sa.DateTime()))
    watermark = bind.execute(sa.select(state.c.reports_folded_through).where(state.c.id == 1)).scalar_one_or_none()
    target = _create("_report_sources_upgraded", typed=True)
    if watermark is None:
        # No fold state means there is no trustworthy boundary for recovering raw.
        bind.execute(
            target.insert().from_select(
                [*KEYS, "first_requested_at", *MEASURES, "unknown_cost_count"],
                sa.select(
                    old.c.bucket_epoch,
                    old.c.account_id,
                    sa.literal(SENTINEL),
                    sa.case((old.c.account_id == SENTINEL, "unknown"), else_="codex"),
                    *(old.c[name] for name in LEGACY_KEYS[2:]),
                    old.c.first_requested_at,
                    *(old.c[name] for name in MEASURES),
                    old.c.request_count,
                ),
            )
        )
    else:
        recovered = _retained_groups(bind, old, watermark)
        totals = (
            sa.select(
                *(recovered.c[name] for name in LEGACY_KEYS),
                *(sa.func.sum(recovered.c[name]).label(name) for name in MEASURES),
            )
            .group_by(*(recovered.c[name] for name in LEGACY_KEYS))
            .subquery()
        )
        _validate_recovery(bind, old, totals)
        remaining_requests = old.c.request_count - sa.func.coalesce(totals.c.request_count, 0)
        residual = (
            sa.select(
                old.c.bucket_epoch,
                old.c.account_id,
                sa.literal(SENTINEL).label("model_source_id"),
                sa.case((old.c.account_id == SENTINEL, "unknown"), else_="codex").label("account_type"),
                *(old.c[name] for name in LEGACY_KEYS[2:]),
                old.c.first_requested_at,
                *((old.c[name] - sa.func.coalesce(totals.c[name], 0)).label(name) for name in MEASURES),
                remaining_requests.label("unknown_cost_count"),
            )
            .select_from(old.outerjoin(totals, sa.and_(*(old.c[name] == totals.c[name] for name in LEGACY_KEYS))))
            .where(remaining_requests > 0)
        )
        # Stage the attributed groups and pruned residual together, then merge any
        # same-Codex-key collisions in one GROUP BY, without replaying old folds.
        columns = [*KEYS, "first_requested_at", *MEASURES, "unknown_cost_count"]
        attributed = sa.select(*(recovered.c[name] for name in columns))
        combined = sa.union_all(residual, attributed).subquery()
        bind.execute(
            target.insert().from_select(
                columns,
                sa.select(
                    *(combined.c[name] for name in KEYS),
                    sa.func.min(combined.c.first_requested_at),
                    *(sa.func.sum(combined.c[name]) for name in (*MEASURES, "unknown_cost_count")),
                ).group_by(*(combined.c[name] for name in KEYS)),
            )
        )
        op.drop_table(recovered.name)
    # Deliberately leave account_usage_rollup_state and its watermark untouched.
    op.drop_table(TABLE)
    op.rename_table(target.name, TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    current = sa.Table(TABLE, sa.MetaData(), autoload_with=bind)
    target = _create("_report_sources_downgraded", typed=False)
    bind.execute(
        target.insert().from_select(
            [*LEGACY_KEYS, "first_requested_at", *MEASURES],
            sa.select(
                *(current.c[name] for name in LEGACY_KEYS),
                sa.func.min(current.c.first_requested_at),
                *(sa.func.sum(current.c[name]) for name in MEASURES),
            ).group_by(*(current.c[name] for name in LEGACY_KEYS)),
        )
    )
    op.drop_table(TABLE)
    op.rename_table(target.name, TABLE)
