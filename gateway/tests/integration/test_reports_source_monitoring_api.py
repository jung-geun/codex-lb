from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db.models import ModelSource, RequestLog, RequestReportHourlyRollup
from app.db.session import SessionLocal
from app.modules.accounts.usage_time_rollup import to_dimension
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.reports.cache import ReportsCaches
from app.modules.reports.rollup import fold_next_report_slice
from tests.integration.test_reports_api import _make_account

pytestmark = pytest.mark.integration

# June 2 in Seoul starts on June 1 UTC: a UTC-only date implementation loses these rows.
START = datetime(2026, 6, 1, 15)
END = START + timedelta(days=1)
COMMON = [
    ("start_date", "2026-06-02"),
    ("end_date", "2026-06-02"),
    ("timezone", "Asia/Seoul"),
    ("api_key_id", "report-key"),
    ("model", "shared-model"),
    ("useragent_group", "OMP"),
]


def _source(source_id: str, provider: str = "claude") -> ModelSource:
    return ModelSource(
        id=source_id,
        name=f"Source {source_id}",
        base_url="https://api.anthropic.com" if provider == "claude" else "https://api.openai.com/v1",
        provider=provider,
        kind="anthropic_compatible" if provider == "claude" else "openai_compatible",
        billing_mode="subscription" if provider == "claude" else "metered",
    )


def _log(
    request_id: str,
    *,
    source_id: str | None = "shared",
    provider: str | None = "claude",
    minutes: int = 0,
    cost: float | None = 0.25,
    model: str = "shared-model",
    key: str = "report-key",
    useragent: str = "OMP",
    requested_at: datetime | None = None,
    request_kind: str = "normal",
) -> RequestLog:
    return RequestLog(
        request_id=request_id,
        account_id="shared" if source_id is None else None,
        model_source_id=source_id,
        provider=provider,
        billing_mode="subscription" if source_id is not None else None,
        model=model,
        api_key_id=key,
        useragent_group=useragent,
        conversation_id="shared-thread",
        requested_at=requested_at or START + timedelta(minutes=minutes),
        request_kind=request_kind,
        status="success",
        input_tokens=10_000 + minutes * 1_000,
        output_tokens=10,
        cached_input_tokens=5_000,
        cost_usd=cost,
    )


async def _seed() -> None:
    async with SessionLocal() as session:
        account = _make_account("shared", "shared@example.com")
        account.alias = "Legacy Codex"
        session.add_all([account, _source("shared"), _source("second"), _source("metered", "openai")])
        await session.flush()
        session.add_all(
            [
                _log("source-known", minutes=0),
                _log("codex-known", source_id=None, provider=None, minutes=1, cost=0.5),
                _log("source-unknown", minutes=2, cost=None),
                _log("source-known-zero", minutes=3, cost=0.0, requested_at=START + timedelta(hours=2)),
                # Same fold dimensions except source identity: these MUST NOT merge.
                _log("second-source", source_id="second", minutes=4, cost=0.75),
                _log("metered-provider", source_id="metered", provider="openai", model="api-model", cost=2.0),
                _log("wrong-key", key="other-key", cost=100.0),
                _log("wrong-model", model="other-model", cost=100.0),
                _log("wrong-agent", useragent="SDK", cost=100.0),
                _log("before-local-day", requested_at=START - timedelta(seconds=1), cost=None),
                _log("after-local-day", requested_at=END, cost=100.0),
                _log("internal-warmup", request_kind="warmup", cost=100.0),
            ]
        )
        await session.commit()


async def _get(client, path: str = "", filters: list[tuple[str, str]] | None = None):
    response = await client.get(f"/api/reports{path}", params=COMMON + (filters or []))
    assert response.status_code == 200, response.text
    return response.json()


def _assert_totals(payload, *, requests: int, cost: float, unknown: int, accounts: int) -> None:
    assert payload["summary"]["totalRequests"] == requests
    assert payload["summary"]["totalCostUsd"] == cost
    assert payload["summary"]["unknownCostCount"] == unknown
    assert payload["summary"]["activeAccounts"] == accounts
    assert payload["daily"][0]["date"] == "2026-06-02"
    for rows in (payload["daily"], payload["byAccount"], payload["byModel"], payload["byUseragent"], payload["byType"]):
        assert sum(row["requests"] for row in rows) == requests
        assert sum(row["costUsd"] for row in rows) == pytest.approx(cost)
        assert sum(row["unknownCostCount"] for row in rows) == unknown


async def test_source_reports_keep_colliding_codex_identity_and_unknown_price_counts(async_client, db_setup):
    await _seed()
    payload = await _get(async_client)
    _assert_totals(payload, requests=5, cost=1.5, unknown=1, accounts=3)
    assert payload["byAccount"] == [
        {"accountId": "source:second", "alias": "Source second", "accountType": "claude",
         "modelSourceId": "second", "costUsd": 0.75, "requests": 1, "unknownCostCount": 0},
        {"accountId": "shared", "alias": "Legacy Codex", "accountType": "codex",
         "modelSourceId": None, "costUsd": 0.5, "requests": 1, "unknownCostCount": 0},
        {"accountId": "source:shared", "alias": "Source shared", "accountType": "claude",
         "modelSourceId": "shared", "costUsd": 0.25, "requests": 3, "unknownCostCount": 1},
    ]
    assert payload["byType"] == [
        {"accountType": "claude", "costUsd": 1.0, "requests": 4, "unknownCostCount": 1},
        {"accountType": "codex", "costUsd": 0.5, "requests": 1, "unknownCostCount": 0},
    ]
    assert payload["comparison"]["previous"]["unknownCostCount"] == 1


@pytest.mark.parametrize(
    ("filters", "requests", "cost", "unknown", "accounts"),
    [
        ([("account_id", "source:shared")], 3, 0.25, 1, 1),
        ([("account_id", "shared")], 1, 0.5, 0, 1),
        ([("account_type", "claude")], 4, 1.0, 1, 2),
        ([("account_type", "codex")], 1, 0.5, 0, 1),
        ([("account_id", "shared"), ("account_id", "source:shared"),
          ("account_type", "codex"), ("account_type", "claude")], 4, 0.75, 1, 2),
    ],
)
async def test_source_report_filters_intersect_with_date_timezone_model_key_and_agent(
    async_client, db_setup, filters, requests, cost, unknown, accounts,
):
    await _seed()
    payload = await _get(async_client, filters=filters)
    _assert_totals(payload, requests=requests, cost=cost, unknown=unknown, accounts=accounts)
    options = await _get(async_client, "/options", filters)
    assert options["models"] == ["shared-model"]
    assert options["useragents"] == ["OMP"]
    assert {row["accountId"] for row in options["accounts"]} == {row["accountId"] for row in payload["byAccount"]}
    assert options["accountTypes"] == sorted({row["accountType"] for row in payload["byType"]})
    thread = await _get(async_client, "/thread-identity", filters)
    assert thread["totalRequests"] == requests
    assert thread["keyed"]["requests"] == requests
    assert thread["keyed"]["unattributedRequestShare"] == 0.0


async def test_thread_identity_counts_sources_separately_and_honors_selected_scope(async_client, db_setup):
    await _seed()
    all_traffic = await _get(async_client, "/thread-identity")
    assert all_traffic["totalRequests"] == 5
    assert all_traffic["keyed"]["meanAccountsPerConversation"] == 3.0
    assert all_traffic["keyed"]["turns"] == 3
    assert all_traffic["keyed"]["accountSwitchRate"] == 1.0
    assert all_traffic["keyed"]["cacheSampleInputTokens"] == 60_000
    source_only = await _get(
        async_client, "/thread-identity", [("account_id", "source:shared"), ("account_type", "claude")],
    )
    assert source_only["totalRequests"] == 3
    assert source_only["keyed"]["conversations"] == 1
    assert source_only["keyed"]["meanAccountsPerConversation"] == 1.0
    assert source_only["keyed"]["accountSwitchRate"] == 0.0
    assert source_only["keyed"]["cacheSampleInputTokens"] == 35_000


async def test_all_report_endpoint_caches_isolate_account_types_and_combined_filters(async_client, db_setup):
    await _seed()
    # Reuse one live app/cache; warming an unfiltered scope must not poison any narrower scope.
    for path in ("", "/options", "/thread-identity"):
        broad = await _get(async_client, path)
        claude = await _get(async_client, path, [("account_type", "claude")])
        codex = await _get(async_client, path, [("account_type", "codex")])
        assert await _get(async_client, path, [("account_type", "claude")]) == claude
        assert await _get(async_client, path) == broad
        if path == "/options":
            assert claude["accountTypes"] == ["claude"]
            assert codex["accountTypes"] == ["codex"]
        elif path == "/thread-identity":
            assert claude["totalRequests"] == 4
            assert codex["totalRequests"] == 1
        else:
            _assert_totals(claude, requests=4, cost=1.0, unknown=1, accounts=2)
            _assert_totals(codex, requests=1, cost=0.5, unknown=0, accounts=1)
        mismatch = await _get(async_client, path, [("account_id", "shared"), ("account_type", "claude")])
        if path == "/options":
            assert mismatch == {"models": [], "useragents": [], "accounts": [], "accountTypes": []}
        elif path == "/thread-identity":
            assert mismatch["totalRequests"] == 0
        else:
            assert mismatch["summary"]["totalRequests"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key_id": "other-key"},
        {"model": "other-model"},
        {"useragent_group": "SDK"},
        {"timezone": "UTC"},
        {"start_date": "2026-06-03", "end_date": "2026-06-03"},
    ],
)
async def test_report_caches_include_each_existing_filter_dimension(async_client, db_setup, overrides):
    await _seed()
    scope = [("account_id", "source:shared"), ("account_type", "claude")]
    query = [(name, overrides.get(name, value)) for name, value in COMMON] + scope
    for path in ("", "/options", "/thread-identity"):
        await _get(async_client, path, scope)
        response = await async_client.get(f"/api/reports{path}", params=query)
        assert response.status_code == 200, response.text
        payload = response.json()
        if path == "/options":
            assert payload["models"] == [overrides.get("model", "shared-model")]
            assert payload["useragents"] == [overrides.get("useragent_group", "OMP")]
            assert payload["accounts"] == [
                {"accountId": "source:shared", "alias": "Source shared", "accountType": "claude"}
            ]
        elif path == "/thread-identity":
            assert payload["totalRequests"] == 1
        else:
            assert payload["summary"]["totalRequests"] == 1
            assert payload["summary"]["totalCostUsd"] == 100.0
            assert payload["summary"]["unknownCostCount"] == 0


@pytest.mark.parametrize("costs", [[None], [0.0], [None, 0.0]])
async def test_generic_provider_type_and_unknown_only_cost_are_reported(async_client, db_setup, costs):
    async with SessionLocal() as session:
        session.add(_source("custom-source", "custom-provider"))
        session.add_all([
            _log(f"custom-{index}", source_id="custom-source", provider="custom-provider", cost=cost)
            for index, cost in enumerate(costs)
        ])
        await session.commit()
    scope = [("account_type", "custom-provider"), ("account_id", "source:custom-source")]
    payload = await _get(async_client, filters=scope)
    unknown = costs.count(None)
    _assert_totals(payload, requests=len(costs), cost=0.0, unknown=unknown, accounts=1)
    assert payload["byType"] == [
        {"accountType": "custom-provider", "costUsd": 0.0, "requests": len(costs), "unknownCostCount": unknown}
    ]
    options = await _get(async_client, "/options", scope)
    assert options["accounts"] == [
        {"accountId": "source:custom-source", "alias": "Source custom-source", "accountType": "custom-provider"}
    ]
    assert options["accountTypes"] == ["custom-provider"]


async def test_source_report_identity_survives_fold_prune_and_source_deletion(async_client, app_instance, db_setup):
    await _seed()
    scopes = [[], [("account_id", "source:shared")], [("account_type", "claude")], [("account_id", "shared")]]

    async def snapshot():
        # Force a new consumer load after storage lifecycle changes, not a stale cached success.
        app_instance.state.reports_caches = ReportsCaches()
        result = []
        for scope in scopes:
            report = await _get(async_client, filters=scope)
            report.pop("generatedAt")
            result.append((report, await _get(async_client, "/options", scope)))
        return result

    before = await snapshot()
    async with SessionLocal() as session:
        # Keep part of the selected day raw to exercise the folded/raw union first.
        while await fold_next_report_slice(session, START + timedelta(hours=1)):
            pass
    assert await snapshot() == before
    async with SessionLocal() as session:
        while await fold_next_report_slice(session, END):
            pass
        source_rows = (await session.scalars(select(RequestReportHourlyRollup).where(
            RequestReportHourlyRollup.model_source_id == to_dimension("shared"),
            RequestReportHourlyRollup.account_type == "claude",
        ))).all()
        assert source_rows
        assert sum(row.unknown_cost_count for row in source_rows) == 2  # selected day + previous day
        await session.execute(delete(RequestLog).where(RequestLog.requested_at < END))
        await session.commit()
    assert await snapshot() == before
    async with SessionLocal() as session:
        assert await ModelSourcesRepository(session).delete("shared") is True
    after = await snapshot()
    for report, options in before:
        for row in report["byAccount"]:
            if row["modelSourceId"] == "shared":
                row["alias"] = None
        for row in options["accounts"]:
            if row["accountId"] == "source:shared":
                row["alias"] = None
    assert after == before
    deleted_report, deleted_options = after[1]
    _assert_totals(deleted_report, requests=3, cost=0.25, unknown=1, accounts=1)
    assert deleted_options["accounts"] == [
        {"accountId": "source:shared", "alias": None, "accountType": "claude"}
    ]


async def test_source_recorded_provider_types_remain_separate_after_pruning(async_client, app_instance, db_setup):
    async with SessionLocal() as session:
        session.add(_source("shared"))
        session.add_all([
            _log("current-provider", cost=0.25),
            _log("historical-provider", provider="previous-provider", cost=None),
        ])
        await session.commit()
    scope = [("account_id", "source:shared")]
    before = await _get(async_client, filters=scope)
    before.pop("generatedAt")
    _assert_totals(before, requests=2, cost=0.25, unknown=1, accounts=1)
    assert before["byType"] == [
        {"accountType": "claude", "costUsd": 0.25, "requests": 1, "unknownCostCount": 0},
        {"accountType": "previous-provider", "costUsd": 0.0, "requests": 1, "unknownCostCount": 1},
    ]
    options_before = await _get(async_client, "/options")
    assert options_before["accounts"] == [
        {"accountId": "source:shared", "alias": "Source shared", "accountType": "claude"}
    ]
    assert options_before["accountTypes"] == ["claude", "previous-provider"]
    async with SessionLocal() as session:
        while await fold_next_report_slice(session, END):
            pass
        await session.execute(delete(RequestLog))
        await session.commit()
    app_instance.state.reports_caches = ReportsCaches()
    after = await _get(async_client, filters=scope)
    after.pop("generatedAt")
    assert after == before
    assert await _get(async_client, "/options") == options_before
    historical_scope = scope + [("account_type", "previous-provider")]
    historical = await _get(async_client, filters=historical_scope)
    _assert_totals(historical, requests=1, cost=0.0, unknown=1, accounts=1)
    options = await _get(async_client, "/options", historical_scope)
    assert options["accountTypes"] == ["previous-provider"]
    assert options["accounts"] == [
        {"accountId": "source:shared", "alias": "Source shared", "accountType": "previous-provider"}
    ]
