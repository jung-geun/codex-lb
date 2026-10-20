from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.auth.dashboard_access import Permission, guest_principal, legacy_permissions
from app.core.auth.dependencies import require_dashboard_permission, validate_dashboard_session
from app.db.session import get_session
from app.modules.quota_estimates import api
from app.modules.quota_estimates.batch import _request_usage, estimate_quotas_batch, native_window_key
from app.modules.quota_estimates.schemas import AccountQuotaEstimates, QuotaEstimate, QuotaEstimatesResponse

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
LEFT = NOW - timedelta(minutes=20)
RESET = NOW + timedelta(hours=2)


def row(**kwargs):
    return SimpleNamespace(**kwargs)


class BatchSession:
    """Script SQL result sets without DB fixtures or mutation of real tables."""

    def __init__(self, batches):
        self.batches = iter(batches)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        rows = next(self.batches)
        return SimpleNamespace(all=lambda: rows)


def account(account_id, seat="same-seat"):
    return row(id=account_id, chatgpt_account_id="workspace", chatgpt_user_id=seat)


def source(source_id, provider="claude", group="shared", seat="source-seat"):
    return row(id=source_id, provider=provider, quota_group=group, account_uuid=seat, organization_uuid="org")


def quota(source_id, at, ratio, model=None):
    return row(
        source_id=source_id, window_key="five_hour", model=model, used_ratio=ratio, resets_at=RESET, observed_at=at
    )


def log(
    log_id,
    *,
    account_id=None,
    source_id=None,
    request_id=None,
    model="actual",
    served_model=None,
    at=None,
    latency_ms=1000,
    input_tokens=800,
    output_tokens=200,
    **kwargs,
):
    values = dict(
        id=log_id,
        account_id=account_id,
        model_source_id=source_id,
        request_id=request_id or f"req-{log_id}",
        requested_at=at or LEFT + timedelta(minutes=2),
        latency_ms=latency_ms,
        model=model,
        served_model=served_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=500,
        cache_creation_input_tokens=200,
        reasoning_tokens=100,
        status="success",
    )
    values.update(kwargs)
    return row(**values)


@pytest.mark.asyncio
async def test_batch_shared_identity_scope_dedup_and_consumer_visible_unknown_windows():
    native = [
        row(
            account_id="a",
            window="primary",
            used_percent=10,
            window_minutes=300,
            reset_at=int(RESET.timestamp()),
            recorded_at=LEFT,
        ),
        row(
            account_id="b",
            window="primary",
            used_percent=30,
            window_minutes=300,
            reset_at=int(RESET.timestamp()),
            recorded_at=NOW,
        ),
    ]
    extra = [
        row(
            account_id="a",
            quota_key="unsupported_meter",
            limit_name="unsupported_meter",
            metered_feature="unsupported_meter",
            window="primary",
            used_percent=20,
            reset_at=int(RESET.timestamp()),
            recorded_at=NOW,
            window_minutes=300,
        )
    ]
    history = [
        quota("s1", LEFT, 0.1),
        quota("s2", NOW, 0.3),
        quota("s1", LEFT, 0.1, "public"),
        quota("s2", NOW, 0.3, "public"),
    ]
    logs = [
        log(1, account_id="a", input_tokens=400, output_tokens=100),
        log(2, account_id="b", input_tokens=400, output_tokens=100),
        # Same workspace is not the same seat. Never mix its large usage.
        log(3, account_id="c", input_tokens=40000, output_tokens=10000),
        log(4, source_id="s1", request_id="duplicate", model="public", input_tokens=None),
        log(5, source_id="s1", request_id="duplicate", model="public"),
        log(6, source_id="s2", served_model="actual"),
        # Not a matching scoped model, but global quotas include the call.
        log(7, source_id="s2", served_model="other", input_tokens=1600, output_tokens=400),
    ]
    session = BatchSession(
        [
            [account("a"), account("b"), account("c", "different-seat")],
            native,
            extra,
            [source("s1"), source("s2"), source("no-data", provider="other", group=None, seat=None)],
            [
                row(source_id="s1", model="public", upstream_model="actual"),
                row(source_id="s2", model="public", upstream_model="actual"),
            ],
            history,
            [quota("s1", NOW, 0.3), quota("s2", NOW, 0.3)],
            logs,
        ]
    )
    data = (await estimate_quotas_batch(session, now=NOW)).model_dump(mode="json", by_alias=True)
    accounts = {item["accountId"]: item for item in data["accounts"]}
    primary = next(item for item in accounts["a"]["windows"] if item["windowKey"] == "primary")
    assert primary["estimatedTotalTokens"] == pytest.approx(5000)
    assert primary["observedTokens"] == 1000 and primary["requestCount"] == 2
    assert accounts["a"]["sharedAccountIds"] == accounts["b"]["sharedAccountIds"] == ["a", "b"]
    assert accounts["c"]["sharedAccountIds"] == ["c"]
    assert all(item["estimatedTotalTokens"] is None for item in accounts["c"]["windows"])
    unsupported = next(item for item in accounts["a"]["windows"] if item["model"] == "unsupported_meter")
    assert unsupported["status"] == "unknown" and unsupported["reason"] == "unsupported_scope"
    assert {item["windowKey"] for item in accounts["a"]["windows"]} == {"primary", "unsupported_meter:primary"}
    sources = {item["sourceId"]: item for item in data["sources"]}
    assert sources["s1"]["windows"] == sources["s2"]["windows"]
    assert sources["s1"]["sharedSourceIds"] == ["s1", "s2"]
    global_quota = next(item for item in sources["s1"]["windows"] if item["model"] is None)
    scoped_quota = next(item for item in sources["s1"]["windows"] if item["model"] == "public")
    assert global_quota["estimatedTotalTokens"] == pytest.approx(20000)
    assert scoped_quota["estimatedTotalTokens"] == pytest.approx(10000)
    assert scoped_quota["observedTokens"] == 2000 and scoped_quota["requestCount"] == 2
    assert sources["no-data"]["windows"][0]["reason"] == "no_observations"
    # Constant eight SELECTs for all accounts, sources, scopes and intervals;
    # no hourly chart decimation, lifetime rollups, or per-interval query.
    assert len(session.statements) == 8
    assert sum("FROM request_logs" in sql for sql in session.statements) == 1
    assert all("GROUP BY" not in sql for sql in session.statements)
    assert all("rollup" not in sql for sql in session.statements)
    assert all("credits_balance" not in sql for sql in session.statements)


@pytest.mark.asyncio
async def test_restricted_native_only_batch_does_not_query_or_leak_sources():
    session = BatchSession([[account("a")], [], [], []])
    result = await estimate_quotas_batch(session, now=NOW, account_ids=["a"], include_sources=False)
    assert not result.sources
    assert result.accounts[0].account_id == "a"
    assert all(window.estimated_total_tokens is None for window in result.accounts[0].windows)
    assert len(session.statements) == 4
    assert all("model_sources" not in sql for sql in session.statements)
    assert "accounts.id IN" in session.statements[0]


@pytest.mark.asyncio
async def test_empty_authorized_account_scope_is_not_all_accounts():
    session = BatchSession([[]])
    result = await estimate_quotas_batch(session, now=NOW, account_ids=[], include_sources=False)
    assert result.accounts == [] and result.sources == []
    assert len(session.statements) == 1


def test_adapter_uses_terminal_log_time_minus_latency_and_respects_actual_served_model():
    request = _request_usage(log(1, model="public", served_model="different"), {"public": "actual"})
    assert request.completed_at == LEFT + timedelta(minutes=2)
    assert request.started_at == request.completed_at - timedelta(seconds=1)
    assert request.model == "different"
    assert _request_usage(log(1, model="public"), {"public": "actual"}).model == "actual"
    assert _request_usage(log(1, model="unmapped"), {}).model is None
    assert _request_usage(log(1, latency_ms=None)).started_at is None
    assert request.input_tokens + request.output_tokens == 1000


@pytest.mark.asyncio
async def test_real_serialization_at_standalone_http_endpoint_is_no_store(monkeypatch):
    result = QuotaEstimatesResponse(
        accounts=[
            AccountQuotaEstimates(
                account_id="a",
                windows=[
                    QuotaEstimate(
                        window_key="primary",
                        status="insufficient",
                        reason="insufficient_delta",
                    )
                ],
            )
        ]
    )
    batch = AsyncMock(return_value=result)
    monkeypatch.setattr(api, "estimate_quotas_batch", batch)
    monkeypatch.setattr(api, "utcnow", lambda: NOW)
    app = FastAPI()
    app.include_router(api.router)
    principal = guest_principal()
    session = object()
    app.dependency_overrides[validate_dashboard_session] = lambda: principal
    app.dependency_overrides[require_dashboard_permission(Permission.ACCOUNTS_READ)] = lambda: principal
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/quota-estimates/")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["sources"] == []
    assert payload["accounts"][0]["windows"][0]["estimatedTotalTokens"] is None
    assert payload["accounts"][0]["windows"][0]["remainingTokens"] is None
    batch.assert_awaited_once_with(session, now=NOW)


@pytest.mark.asyncio
async def test_endpoint_declares_all_scope_accounts_permission_not_dashboard_only():
    # A direct dependency check avoids auth cookies/DB fixtures, but exercises
    # the actual dependency attached to the route and no overridden guard.
    from app.core.auth.dependencies import ensure_dashboard_permission
    from app.core.exceptions import DashboardPermissionError

    dependency = require_dashboard_permission(Permission.ACCOUNTS_READ)
    assert dependency.requirement.permission == Permission.ACCOUNTS_READ
    principal = guest_principal()
    grants = {key: value for key, value in principal.grants.items() if key != Permission.ACCOUNTS_READ}
    unauthorized = replace(principal, grants=grants, permissions=legacy_permissions(grants))
    with pytest.raises(DashboardPermissionError):
        ensure_dashboard_permission(
            unauthorized, dependency.requirement.permission, minimum_scope=dependency.requirement.minimum_scope
        )


@pytest.mark.parametrize(
    "slot,minutes,expected",
    [
        (None, 300, "primary"),
        ("5h", 300, "primary"),
        ("7d", 10080, "secondary"),
        ("primary", 10080, "secondary"),
        ("primary", 43200, "monthly"),
        ("secondary", 43200, "monthly"),
        ("monthly", 43200, "monthly"),
    ],
)
def test_native_windows_use_existing_monthly_and_weekly_primary_semantics(slot, minutes, expected):
    assert native_window_key(slot, minutes) == expected


@pytest.mark.asyncio
async def test_free_monthly_evidence_is_not_mislabeled_or_duplicated_as_five_hour():
    samples = [
        row(
            account_id="free",
            window="primary",
            window_minutes=43200,
            used_percent=ratio,
            reset_at=int(RESET.timestamp()),
            recorded_at=at,
        )
        for at, ratio in [(LEFT, 10), (NOW, 30)]
    ]
    session = BatchSession([[account("free")], samples, [], [log(1, account_id="free")]])
    result = await estimate_quotas_batch(session, now=NOW, include_sources=False)
    assert len(result.accounts[0].windows) == 1
    quota = result.accounts[0].windows[0]
    assert quota.window_key == "monthly" and quota.status == "estimated"
    assert quota.estimated_total_tokens == pytest.approx(5000)


@pytest.mark.asyncio
async def test_model_less_provider_specific_window_is_unknown_not_global():
    samples = [quota("s", LEFT, 0.1), quota("s", NOW, 0.3)]
    for sample in samples:
        sample.window_key = "seven_day_unresolved_family"
    session = BatchSession([[], [source("s")], [], samples, [], [log(1, source_id="s")]])
    result = await estimate_quotas_batch(session, now=NOW)
    quota_result = result.sources[0].windows[0]
    assert quota_result.status == "unknown" and quota_result.reason == "unsupported_scope"
    assert quota_result.estimated_total_tokens is None


@pytest.mark.asyncio
async def test_current_alias_conflicts_do_not_choose_one_arbitrary_provider_ratio():
    session = BatchSession(
        [
            [],
            [source("s1"), source("s2")],
            [],
            [quota("s1", LEFT, 0.1)],
            [quota("s1", NOW, 0.3), quota("s2", NOW, 0.4)],
            [log(1, source_id="s1")],
        ]
    )
    result = await estimate_quotas_batch(session, now=NOW)
    assert all(item.windows[0].reason == "inconsistent_observations" for item in result.sources)
    assert all(item.windows[0].estimated_total_tokens is None for item in result.sources)
