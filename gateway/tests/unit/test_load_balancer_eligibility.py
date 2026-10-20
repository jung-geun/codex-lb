from __future__ import annotations

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.modules.proxy.load_balancer as load_balancer_module
from app.core.balancer import HEALTH_TIER_HEALTHY
from app.core.crypto import TokenEncryptor
from app.core.openai.model_registry import ModelRegistry, ModelRegistrySnapshot
from app.db.models import Account, AccountStatus, UsageHistory
from app.db.snapshot import clone_row
from app.modules.proxy.account_cache import AccountSelectionCache
from app.modules.proxy.load_balancer import AccountConcurrencyCaps, AccountLease, LoadBalancer, RuntimeState
from app.modules.proxy.repo_bundle import ProxyRepositories
from tests.simulation.virtual_time import VirtualClock

pytestmark = pytest.mark.unit

_MODEL = "snapshot-model"
_NOW = 1_800_000_000.0


@dataclass
class _Snapshot:
    balancer: LoadBalancer
    accounts: list[Account]
    primary: dict[str, UsageHistory]
    secondary: dict[str, UsageHistory]
    monthly: dict[str, UsageHistory]
    clock: VirtualClock
    accounts_repo: MagicMock
    sticky_repo: MagicMock
    registry: ModelRegistry


@pytest.fixture
def snapshot(monkeypatch: pytest.MonkeyPatch) -> _Snapshot:
    clock = VirtualClock(epoch_value=_NOW, monotonic_value=1000.0)
    encryptor = TokenEncryptor()
    accounts = [
        Account(
            id="account-a",
            chatgpt_account_id="workspace-a",
            email="snapshot@example.com",
            plan_type="plus",
            access_token_encrypted=encryptor.encrypt("access"),
            refresh_token_encrypted=encryptor.encrypt("refresh"),
            id_token_encrypted=encryptor.encrypt("id"),
            last_refresh=clock.now(),
            status=AccountStatus.ACTIVE,
            security_work_authorized=False,
            routing_policy="normal",
        )
    ]
    primary: dict[str, UsageHistory] = {}
    secondary: dict[str, UsageHistory] = {}
    monthly: dict[str, UsageHistory] = {}
    accounts_repo = MagicMock()
    accounts_repo.list_accounts = AsyncMock(return_value=accounts)
    sticky_repo = MagicMock()

    async def latest_by_account(window: str | None = None) -> dict[str, UsageHistory]:
        if window == "secondary":
            return secondary
        if window == "monthly":
            return monthly
        return primary

    @asynccontextmanager
    async def repo_factory() -> AsyncIterator[ProxyRepositories]:
        yield cast(
            ProxyRepositories,
            SimpleNamespace(
                accounts=accounts_repo,
                usage=SimpleNamespace(latest_by_account=latest_by_account),
                sticky_sessions=sticky_repo,
            ),
        )

    registry = ModelRegistry()
    registry._snapshot = ModelRegistrySnapshot(
        models={},
        model_plans={_MODEL: frozenset({"plus"})},
        plan_models={"plus": frozenset({_MODEL})},
        model_service_tier_plans={_MODEL: {"priority": frozenset({"plus"})}},
        model_service_tier_accounts={_MODEL: {"priority": frozenset({"account-a"})}},
        account_plans={"account-a": "plus"},
        fetched_at=clock.time(),
        model_accounts={_MODEL: frozenset({"account-a"})},
        account_catalogs_authoritative=True,
    )
    monkeypatch.setattr(load_balancer_module, "get_model_registry", lambda: registry)
    monkeypatch.setattr(
        load_balancer_module, "get_account_selection_cache", lambda: AccountSelectionCache(ttl_seconds=0),
    )
    monkeypatch.setattr(load_balancer_module, "are_all_account_circuit_breakers_open", lambda: False)
    monkeypatch.setattr(
        load_balancer_module,
        "effective_account_concurrency_caps",
        lambda dashboard_settings=None: AccountConcurrencyCaps(2, 3),
    )
    balancer = LoadBalancer(repo_factory, encryptor=encryptor, clock=clock)
    return _Snapshot(balancer, accounts, primary, secondary, monthly, clock, accounts_repo, sticky_repo, registry)


def _usage(account_id: str, *, window: str, used_percent: float) -> UsageHistory:
    return UsageHistory(
        id=1,
        account_id=account_id,
        recorded_at=datetime.fromtimestamp(_NOW, tz=timezone.utc),
        window=window,
        used_percent=used_percent,
        reset_at=int(_NOW + 3600),
        window_minutes=300 if window == "primary" else 10080,
    )


@pytest.mark.asyncio
async def test_healthy_account_is_eligible_without_creating_runtime(snapshot: _Snapshot) -> None:
    snapshot.primary["account-a"] = _usage("account-a", window="primary", used_percent=10.0)

    assert await snapshot.balancer.has_eligible_account(model=_MODEL)
    assert snapshot.balancer._runtime == {}
    assert snapshot.sticky_repo.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AccountStatus.PAUSED, AccountStatus.DEACTIVATED])
async def test_administratively_unavailable_account_is_not_eligible(snapshot: _Snapshot, status: AccountStatus) -> None:
    snapshot.accounts[0].status = status

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["primary", "secondary"])
async def test_exhausted_account_is_not_eligible(snapshot: _Snapshot, window: str) -> None:
    account = snapshot.accounts[0]
    account.status = AccountStatus.QUOTA_EXCEEDED
    account.reset_at = int(_NOW + 3600)
    rows = snapshot.primary if window == "primary" else snapshot.secondary
    rows[account.id] = _usage(account.id, window=window, used_percent=100.0)

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)
    assert account.status == AccountStatus.QUOTA_EXCEEDED
    assert snapshot.balancer._runtime == {}


@pytest.mark.asyncio
async def test_persisted_rate_limit_is_not_eligible(snapshot: _Snapshot) -> None:
    snapshot.accounts[0].status = AccountStatus.RATE_LIMITED
    snapshot.accounts[0].reset_at = int(_NOW + 60)
    snapshot.accounts[0].blocked_at = _NOW

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
async def test_runtime_cooldown_uses_injected_clock_without_clearing_live_state(snapshot: _Snapshot) -> None:
    runtime = RuntimeState(cooldown_until=_NOW + 10, last_error_at=_NOW, error_count=1)
    snapshot.balancer._runtime["account-a"] = runtime
    before = deepcopy(runtime)

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)
    snapshot.clock.advance(11)
    assert await snapshot.balancer.has_eligible_account(model=_MODEL)
    assert runtime == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("creates", "streams", "eligible"),
    [(0, 0, True), (1, 2, True), (2, 0, False), (0, 3, False), (2, 3, False)],
)
async def test_current_capacity_must_allow_create_and_stream(
    snapshot: _Snapshot, creates: int, streams: int, eligible: bool
) -> None:
    snapshot.balancer._runtime["account-a"] = RuntimeState(
        inflight_response_creates=creates,
        inflight_streams=streams,
    )

    assert await snapshot.balancer.has_eligible_account(model=_MODEL) is eligible


@pytest.mark.asyncio
async def test_explicit_concurrency_caps_override_default_capacity(snapshot: _Snapshot) -> None:
    snapshot.balancer._runtime["account-a"] = RuntimeState(inflight_response_creates=2, inflight_streams=3)

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)
    assert await snapshot.balancer.has_eligible_account(
        model=_MODEL, concurrency_caps=AccountConcurrencyCaps(response_create_limit=3, stream_limit=4)
    )
    assert await snapshot.balancer.has_eligible_account(
        model=_MODEL, concurrency_caps=AccountConcurrencyCaps(response_create_limit=0, stream_limit=0)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_ids", [[], ["missing-account"]])
async def test_assignment_scope_does_not_widen_to_healthy_accounts(snapshot: _Snapshot, account_ids: list[str]) -> None:
    assert not await snapshot.balancer.has_eligible_account(model=_MODEL, account_ids=account_ids)
    assert await snapshot.balancer.has_eligible_account(model=_MODEL, account_ids=["account-a"])


@pytest.mark.asyncio
async def test_model_catalog_scope_excludes_healthy_account_with_same_plan(snapshot: _Snapshot) -> None:
    registry_snapshot = snapshot.registry.get_snapshot()
    assert registry_snapshot is not None
    registry_snapshot.model_accounts[_MODEL] = frozenset({"another-account"})

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
async def test_model_plan_scope_is_preserved(snapshot: _Snapshot) -> None:
    registry_snapshot = snapshot.registry.get_snapshot()
    assert registry_snapshot is not None
    registry_snapshot.model_plans[_MODEL] = frozenset({"pro"})
    registry_snapshot.account_plans.clear()

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
async def test_service_tier_scope_is_preserved(snapshot: _Snapshot) -> None:
    assert await snapshot.balancer.has_eligible_account(model=_MODEL, service_tier="priority")
    registry_snapshot = snapshot.registry.get_snapshot()
    assert registry_snapshot is not None
    registry_snapshot.model_service_tier_accounts[_MODEL]["priority"] = frozenset({"another-account"})

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL, service_tier="priority")
    assert await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
async def test_security_authorization_is_required_only_when_requested(snapshot: _Snapshot) -> None:
    assert await snapshot.balancer.has_eligible_account(model=_MODEL)
    assert not await snapshot.balancer.has_eligible_account(model=_MODEL, require_security_work_authorized=True)
    snapshot.accounts[0].security_work_authorized = True
    assert await snapshot.balancer.has_eligible_account(model=_MODEL, require_security_work_authorized=True)


@pytest.mark.asyncio
async def test_dashboard_circuit_toggle_controls_eligibility(
    snapshot: _Snapshot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(load_balancer_module, "are_all_account_circuit_breakers_open", lambda: True)

    assert not await snapshot.balancer.has_eligible_account(
        model=_MODEL, dashboard_settings=SimpleNamespace(circuit_breaker_enabled=True)
    )
    assert await snapshot.balancer.has_eligible_account(
        model=_MODEL, dashboard_settings=SimpleNamespace(circuit_breaker_enabled=False)
    )


@pytest.mark.asyncio
async def test_stale_capacity_uses_dashboard_ttl_without_reclaiming_live_lease(snapshot: _Snapshot) -> None:
    lease = AccountLease("lease", "account-a", "response_create", acquired_at=snapshot.clock.monotonic() - 31)
    runtime = RuntimeState(inflight_response_creates=1, leases={lease.lease_id: lease}, last_selected_at=_NOW - 31)
    snapshot.balancer._runtime["account-a"] = runtime
    before = deepcopy(runtime)
    caps = AccountConcurrencyCaps(response_create_limit=1, stream_limit=1)

    assert not await snapshot.balancer.has_eligible_account(
        model=_MODEL,
        concurrency_caps=caps,
        dashboard_settings=SimpleNamespace(proxy_account_lease_ttl_seconds=60),
    )
    assert await snapshot.balancer.has_eligible_account(
        model=_MODEL,
        concurrency_caps=caps,
        dashboard_settings=SimpleNamespace(proxy_account_lease_ttl_seconds=30),
    )
    assert runtime == before
    assert snapshot.balancer._routing_tunables is None


@pytest.mark.asyncio
async def test_snapshot_does_not_mutate_order_health_ownership_or_random_state(
    snapshot: _Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot.primary["account-a"] = _usage("account-a", window="primary", used_percent=99.0)
    snapshot.balancer._runtime["account-a"] = RuntimeState(
        health_tier=HEALTH_TIER_HEALTHY,
        last_selected_at=_NOW - 100,
        version=7,
        health_version=3,
        ttft_samples=[(_NOW - 1, 20)],
        tps_samples={_MODEL: [(_NOW - 1, 10.0)]},
    )
    snapshot.balancer._runtime["outside-scope"] = RuntimeState(last_selected_at=_NOW - 50)
    before = deepcopy(snapshot.balancer._runtime)
    random_state = random.getstate()
    build_states = load_balancer_module._build_states

    def locked_build_states(**kwargs: Any) -> Any:
        assert snapshot.balancer._runtime_lock.locked()
        return build_states(**kwargs)

    monkeypatch.setattr(load_balancer_module, "_build_states", locked_build_states)
    for _ in range(2):
        assert await snapshot.balancer.has_eligible_account(model=_MODEL)

    assert snapshot.balancer._runtime == before
    assert random.getstate() == random_state
    assert snapshot.sticky_repo.mock_calls == []
    assert snapshot.accounts[0].status == AccountStatus.ACTIVE
    assert snapshot.accounts_repo.method_calls == [
        ("list_accounts", (), {}),
        ("list_accounts", (), {}),
    ]


def _add_second_account(snapshot: _Snapshot) -> Account:
    account = clone_row(snapshot.accounts[0])
    account.id = "account-b"
    snapshot.accounts.append(account)
    registry_snapshot = snapshot.registry.get_snapshot()
    assert registry_snapshot is not None
    registry_snapshot.account_plans[account.id] = "plus"
    registry_snapshot.model_accounts[_MODEL] = frozenset({"account-a", account.id})
    return account


@pytest.mark.asyncio
async def test_empty_account_pool_is_not_eligible(snapshot: _Snapshot) -> None:
    snapshot.accounts.clear()

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
async def test_full_scoped_account_does_not_borrow_unassigned_capacity(snapshot: _Snapshot) -> None:
    _add_second_account(snapshot)
    snapshot.balancer._runtime["account-a"] = RuntimeState(inflight_streams=3)

    assert await snapshot.balancer.has_eligible_account(model=_MODEL)
    assert not await snapshot.balancer.has_eligible_account(model=_MODEL, account_ids=["account-a"])
    assert await snapshot.balancer.has_eligible_account(model=_MODEL, account_ids=["account-b"])


@pytest.mark.asyncio
async def test_create_and_stream_slots_cannot_be_combined_across_accounts(snapshot: _Snapshot) -> None:
    _add_second_account(snapshot)
    snapshot.balancer._runtime["account-a"] = RuntimeState(inflight_response_creates=2)
    snapshot.balancer._runtime["account-b"] = RuntimeState(inflight_streams=3)

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL)


@pytest.mark.asyncio
async def test_dashboard_snapshot_is_passed_to_cap_resolution(
    snapshot: _Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = SimpleNamespace(proxy_account_response_create_limit=1, proxy_account_stream_limit=1)
    snapshot.balancer._runtime["account-a"] = RuntimeState(inflight_streams=1)
    resolve_caps = MagicMock(return_value=AccountConcurrencyCaps(response_create_limit=1, stream_limit=1))
    monkeypatch.setattr(load_balancer_module, "effective_account_concurrency_caps", resolve_caps)

    assert not await snapshot.balancer.has_eligible_account(model=_MODEL, dashboard_settings=dashboard)
    resolve_caps.assert_called_once_with(dashboard)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,rank", [("burn_first", 0), ("normal", 1), ("preserve", 2)])
async def test_eligible_account_snapshot_exposes_real_policy_rank(snapshot: _Snapshot, policy: str, rank: int) -> None:
    snapshot.accounts[0].routing_policy = policy
    assert await snapshot.balancer.eligible_account_routing_rank(model=_MODEL) == rank
    assert snapshot.balancer._runtime == {}
