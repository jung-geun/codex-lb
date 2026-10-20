from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.openai.model_registry import get_model_registry
from app.core.utils.time import naive_utc_to_epoch, utcnow
from app.db.models import (
    Account,
    AccountStatus,
    AdditionalUsageHistory,
    ModelSource,
    ModelSourceModel,
    ModelSourceOAuthCredential,
    ModelSourceQuotaWindow,
    UsageHistory,
)
from app.db.session import SessionLocal
from app.modules.proxy.additional_model_limits import get_additional_quota_key_for_model_id

pytestmark = pytest.mark.integration
NATIVE = "gpt-5.5"
CLAUDE = "anthropic/claude-sonnet-quota-test"


async def native_account(
    account_id: str,
    *,
    used: float = 20,
    minutes: int = 300,
    seat: str | None = None,
    status: AccountStatus = AccountStatus.ACTIVE,
    age: int = 0,
) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            Account(
                id=account_id,
                chatgpt_account_id="private-shared-workspace",
                chatgpt_user_id=seat or account_id,
                email=f"{account_id}@private.example",
                plan_type="pro",
                status=status,
                access_token_encrypted=b"private-access",
                refresh_token_encrypted=b"private-refresh",
                id_token_encrypted=b"private-id",
                last_refresh=now,
            )
        )
        await session.flush()
        session.add(
            UsageHistory(
                account_id=account_id,
                window="primary",
                used_percent=used,
                window_minutes=minutes,
                recorded_at=now - timedelta(minutes=age),
                reset_at=naive_utc_to_epoch(now + timedelta(days=1)),
            )
        )
        await session.commit()


async def claude_source(
    source_id: str,
    *,
    used: float | None = 0.2,
    principal: str | None = None,
    group: str | None = None,
    model: str = CLAUDE,
    age: int = 0,
    enabled: bool = True,
    restricted: bool = False,
    provider: str = "claude",
) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            ModelSource(
                id=source_id,
                name=f"private-source-{source_id}",
                base_url="https://quota.invalid",
                kind="anthropic_compatible",
                provider=provider,
                auth_type="claude_oauth",
                billing_mode="subscription",
                is_enabled=enabled,
                supports_messages=True,
                supports_chat_completions=False,
                entitlement_status="verified",
                entitlement_verified_at=now,
                extra_usage_enabled=False,
                quota_group=group,
                quota_checked_at=now,
                oauth_credential=ModelSourceOAuthCredential(
                    access_token_encrypted=b"private-claude-secret",
                    expires_at=now + timedelta(hours=1),
                    account_uuid=principal,
                    organization_uuid="private-org",
                    account_label="private@example.test",
                ),
                models=[
                    ModelSourceModel(
                        model=model,
                        upstream_model="claude-sonnet-quota-test",
                        is_enabled=True,
                        subscription_eligible=True,
                        subscription_restricted=restricted,
                    )
                ],
                quota_windows=[
                    ModelSourceQuotaWindow(
                        window_key=key,
                        used_ratio=used,
                        observed_at=now - timedelta(minutes=age),
                        resets_at=now + timedelta(days=1),
                    )
                    for key in ("five_hour", "seven_day")
                ],
            )
        )
        await session.commit()


async def key_headers(client, **policy) -> dict[str, str]:
    created = await client.post("/api/api-keys/", json={"name": "client quota", **policy})
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['key']}"}


async def quota(client, headers, model: str = CLAUDE):
    response = await client.get("/v1/usage/quota", params={"model": model}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json(), {row["window"]: row for row in response.json()["windows"]}


@pytest.mark.asyncio
async def test_client_quota_provider_totals_are_separate_and_private(async_client):
    await native_account("native-a")
    await native_account("native-b", used=40)
    await claude_source("claude-a", principal="claude-seat-a", used=0.5)
    await claude_source("claude-b", principal="claude-seat-b", used=1)
    headers = await key_headers(async_client)
    native, native_windows = await quota(async_client, headers, NATIVE)
    claude, claude_windows = await quota(async_client, headers)
    assert native["provider"] == "codex" and claude["provider"] == "claude"
    assert native_windows["5h"]["remaining_account_equivalents"] == pytest.approx(1.4)
    assert native_windows["5h"]["remaining_percent"] == pytest.approx(70)
    assert claude_windows["7d"]["remaining_account_equivalents"] == 0.5
    assert claude_windows["7d"]["reporting_pools"] == 2
    assert not any(
        secret in str((native, claude))
        for secret in (
            "private",
            "native-a",
            "native-b",
            "claude-seat",
            "claude-a",
            "claude-b",
        )
    )
    legacy = await async_client.get("/v1/usage", headers=headers)
    assert legacy.status_code == 200
    assert "windows" not in legacy.json() and "provider" not in legacy.json()


@pytest.mark.asyncio
async def test_client_quota_intersects_source_and_exact_model_scope(async_client):
    await claude_source("allowed", principal="a", used=0.8)
    await claude_source("unassigned", principal="b", used=0)
    headers = await key_headers(
        async_client, assignedSourceIds=["allowed"], allowedModels=[CLAUDE], enforcedModel=CLAUDE
    )
    _, windows = await quota(async_client, headers)
    assert windows["7d"]["remaining_account_equivalents"] == pytest.approx(0.2)
    denied = await async_client.get("/v1/usage/quota", params={"model": "claude-unallowed"}, headers=headers)
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_client_quota_account_scope_and_authoritative_model_accounts(async_client):
    await native_account("included", used=20)
    await native_account("other", used=0)
    registry = get_model_registry()
    native = registry.get_models_with_fallback()[NATIVE]
    await registry.update({"pro": [native]}, per_account_results={"included": ("pro", [native]), "other": ("pro", [])})
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers, NATIVE)
    assert windows["5h"]["remaining_account_equivalents"] == 0.8
    scoped = await key_headers(async_client, assignedAccountIds=["other"])
    result, _ = await quota(async_client, scoped, NATIVE)
    assert result["windows"] == []


@pytest.mark.asyncio
async def test_client_quota_shared_seats_deduplicate_but_workspace_seats_do_not(async_client):
    await native_account("a", seat="same-seat", used=20)
    await native_account("alias", seat="same-seat", used=20)
    await native_account("b", seat="other-seat", used=40)
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers, NATIVE)
    assert windows["5h"]["reporting_pools"] == 2
    assert windows["5h"]["remaining_account_equivalents"] == pytest.approx(1.4)


@pytest.mark.asyncio
async def test_client_quota_transitive_shared_aliases_never_double_count(async_client):
    await claude_source("a", principal="same-seat", used=0.6)
    await claude_source("bridge", principal="same-seat", group="shared", used=0.6)
    await claude_source("c", group="shared", used=0.6)
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers)
    assert windows["7d"]["reporting_pools"] == 1
    assert windows["7d"]["remaining_account_equivalents"] == 0.4


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stale", "null", "error", "identity", "reset", "conflict"])
async def test_client_quota_uncertain_pool_stays_unknown(async_client, reason):
    await claude_source("known", principal="known", used=0.2)
    await claude_source(
        "uncertain",
        principal=None if reason == "identity" else "uncertain",
        used=None if reason == "null" else 0.1,
        age=6 if reason == "stale" else 0,
        group="conflict" if reason == "conflict" else None,
    )
    async with SessionLocal() as session:
        source = await session.get(ModelSource, "uncertain")
        assert source is not None
        if reason == "error":
            source.quota_error = "provider read failed"
        elif reason == "reset":
            for row in (
                await session.scalars(
                    select(ModelSourceQuotaWindow).where(ModelSourceQuotaWindow.source_id == "uncertain")
                )
            ).all():
                row.resets_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    if reason == "conflict":
        await claude_source("contradictory", principal="different-proven-seat", group="conflict", used=0.1)
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers)
    assert windows["7d"]["remaining_account_equivalents"] == 0.8
    assert windows["7d"]["reporting_pools"] == 1
    assert windows["7d"]["unknown_pools"] == 1
    assert windows["7d"]["remaining_percent"] is None


@pytest.mark.asyncio
async def test_client_quota_excludes_disabled_restricted_paused_and_deactivated(async_client):
    await native_account("paused", status=AccountStatus.PAUSED)
    await native_account("deactivated", status=AccountStatus.DEACTIVATED)
    await native_account("exhausted", used=100, status=AccountStatus.QUOTA_EXCEEDED)
    await claude_source("disabled", principal="disabled", enabled=False)
    await claude_source("restricted", principal="restricted", restricted=True)
    await claude_source("zero", principal="zero", used=1)
    headers = await key_headers(async_client)
    _, native = await quota(async_client, headers, NATIVE)
    _, claude = await quota(async_client, headers)
    for windows in (native, claude):
        row = windows["5h"]
        assert row["remaining_account_equivalents"] == 0
        assert row["reporting_pools"] == 1 and row["unknown_pools"] == 0
        assert row["remaining_percent"] == 0


@pytest.mark.asyncio
async def test_client_quota_respects_selected_model_specific_window(async_client):
    await claude_source("scoped", principal="seat")
    now = utcnow()
    async with SessionLocal() as session:
        session.add_all(
            [
                ModelSourceQuotaWindow(
                    source_id="scoped",
                    window_key="seven_day_sonnet",
                    model="claude-sonnet-quota-test",
                    used_ratio=0.9,
                    observed_at=now,
                    resets_at=now + timedelta(days=1),
                ),
                ModelSourceQuotaWindow(
                    source_id="scoped",
                    window_key="seven_day_opus",
                    model="claude-opus-other",
                    used_ratio=1,
                    observed_at=now,
                    resets_at=now + timedelta(days=1),
                ),
            ]
        )
        await session.commit()
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers)
    assert windows["7d"]["remaining_account_equivalents"] == pytest.approx(0.1)
    assert windows["5h"]["remaining_account_equivalents"] == 0.8


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes,label", [(10080, "7d"), (43200, "30d"), (60, "60m")])
async def test_client_quota_uses_duration_not_native_slot(async_client, minutes, label):
    await native_account("duration", minutes=minutes)
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers, NATIVE)
    assert "5h" not in windows and set(windows) == {label}
    assert windows[label]["remaining_account_equivalents"] == 0.8


@pytest.mark.asyncio
async def test_client_quota_additional_native_model_limit_is_not_ignored(async_client):
    await native_account("spark", used=10)
    model = "gpt-5.3-codex-spark"
    quota_key = get_additional_quota_key_for_model_id(model)
    assert quota_key is not None
    headers = await key_headers(async_client)
    _, missing = await quota(async_client, headers, model)
    assert missing["5h"]["remaining_account_equivalents"] is None
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            AdditionalUsageHistory(
                account_id="spark",
                quota_key=quota_key,
                limit_name=quota_key,
                metered_feature=quota_key,
                window="primary",
                used_percent=100,
                window_minutes=300,
                recorded_at=now,
                reset_at=naive_utc_to_epoch(now + timedelta(hours=1)),
            )
        )
        await session.commit()
    _, present = await quota(async_client, headers, model)
    assert present["5h"]["remaining_account_equivalents"] == 0


@pytest.mark.asyncio
async def test_client_quota_hidden_unknown_and_invalid_requests(async_client):
    await claude_source("private", principal="private")
    private = await key_headers(async_client, usageSections="upstream_limits")
    result, _ = await quota(async_client, private)
    assert result["hidden"] is True and result["provider"] is None and result["windows"] == []
    headers = await key_headers(async_client)
    result, _ = await quota(async_client, headers, "claude-not-configured-or-known")
    assert result["provider"] is None and result["windows"] == []
    for params, auth, expected in [({}, headers, 400), ({"model": "x"}, {}, 401)]:
        response = await async_client.get("/v1/usage/quota", params=params, headers=auth)
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_client_quota_api_rate_windows_are_not_subscription_balances(async_client):
    await claude_source("api", principal="api")
    async with SessionLocal() as session:
        source = await session.get(ModelSource, "api")
        assert source is not None
        source.billing_mode = "api_metered"
        await session.commit()
    headers = await key_headers(async_client)
    result, _ = await quota(async_client, headers)
    assert result["provider"] == "claude" and result["windows"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertainty", ["stale", "identity"])
async def test_client_quota_native_unknown_is_not_zero(async_client, uncertainty):
    await native_account("native-known", used=20)
    await native_account("native-uncertain", used=0, age=6 if uncertainty == "stale" else 0)
    if uncertainty == "identity":
        async with SessionLocal() as session:
            account = await session.get(Account, "native-uncertain")
            assert account is not None
            account.chatgpt_user_id = None
            await session.commit()
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers, NATIVE)
    assert windows["5h"]["remaining_account_equivalents"] == 0.8
    assert windows["5h"]["unknown_pools"] == 1
    assert windows["5h"]["remaining_percent"] is None


@pytest.mark.asyncio
async def test_client_quota_native_reads_are_batched_not_per_account(async_client):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    for index in range(32):
        await native_account(f"batch-{index}")
    headers = await key_headers(async_client)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "from usage_history" in statement.lower():
            statements.append(statement)

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        _, windows = await quota(async_client, headers, NATIVE)
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert windows["5h"]["reporting_pools"] == 32
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_client_quota_equivalent_trailing_slash_and_error_cache_policy(async_client, app_instance, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.modules.proxy import api as proxy_api

    await claude_source("slash", principal="slash")
    headers = await key_headers(async_client)
    canonical, _ = await quota(async_client, headers)
    slash = await async_client.get(
        "/v1/usage/quota/", params={"model": CLAUDE}, headers=headers, follow_redirects=False
    )
    assert slash.status_code == 200 and slash.headers["cache-control"] == "no-store"
    assert slash.json()["windows"] == canonical["windows"]

    async def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic private backend error")

    monkeypatch.setattr(proxy_api, "build_client_quota", fail)
    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False), base_url="http://testserver"
    ) as client:
        failed = await client.get("/v1/usage/quota", params={"model": CLAUDE}, headers=headers)
    assert failed.status_code == 500
    assert failed.headers["cache-control"] == "no-store"
    assert "synthetic private" not in failed.text


@pytest.mark.asyncio
async def test_client_quota_spark_replaces_standard_pool_even_when_standard_exhausted(async_client):
    await native_account("spark-independent", used=100, status=AccountStatus.QUOTA_EXCEEDED)
    model = "gpt-5.3-codex-spark"
    quota_key = get_additional_quota_key_for_model_id(model)
    assert quota_key is not None
    now = utcnow()
    async with SessionLocal() as session:
        session.add(
            AdditionalUsageHistory(
                account_id="spark-independent",
                quota_key=quota_key,
                limit_name=quota_key,
                metered_feature=quota_key,
                window="primary",
                used_percent=20,
                window_minutes=300,
                recorded_at=now,
                reset_at=naive_utc_to_epoch(now + timedelta(hours=1)),
            )
        )
        await session.commit()
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers, model)
    assert windows["5h"]["remaining_account_equivalents"] == 0.8


@pytest.mark.asyncio
async def test_client_quota_weekly_only_account_does_not_acquire_five_hour_window(async_client):
    await native_account("weekly", used=20, minutes=10080)
    await native_account("five-hour", used=40, minutes=300)
    headers = await key_headers(async_client)
    _, windows = await quota(async_client, headers, NATIVE)
    assert windows["5h"]["remaining_account_equivalents"] == 0.6
    assert windows["5h"]["reporting_pools"] == 1 and windows["5h"]["unknown_pools"] == 0
    assert windows["7d"]["remaining_account_equivalents"] == 0.8


@pytest.mark.asyncio
async def test_client_quota_canonical_native_alias_does_not_count_denied_source_alias(async_client):
    await native_account("native-alias", used=20)
    alias = "gpt-5.5-high"
    # Native ACLs normalize reasoning aliases; source routing intentionally
    # requires the exact public registration. This Claude registration must
    # neither become an unknown pool nor erase the native Codex family.
    await claude_source("denied-source-alias", principal="private-other-provider", model=alias)
    headers = await key_headers(async_client, allowedModels=[NATIVE])
    result, windows = await quota(async_client, headers, alias)
    assert result["provider"] == "codex" and result["model"] == alias
    assert windows["5h"]["remaining_account_equivalents"] == 0.8
    assert windows["5h"]["reporting_pools"] == 1 and windows["5h"]["unknown_pools"] == 0
