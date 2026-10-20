"""Behavioral auth regressions using the pinned upstream's isolated SQLite fixtures.

Only the upstream token exchange is replaced: status persistence, credential
compare-and-set, and subsequent account selection use the real implementations.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.modules.proxy.service as proxy_service
from app.core.auth.refresh import RefreshError
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus
from app.db.session import SessionLocal
from app.modules.accounts import auth_manager as auth_manager_module
from app.modules.accounts.auth_manager import AuthManager
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.proxy.account_cache import mark_account_routing_unavailable
from app.modules.proxy.load_balancer import LoadBalancer
from app.modules.proxy.repo_bundle import ProxyRepositories
from app.modules.proxy.sticky_repository import StickySessionsRepository
from app.modules.request_logs.repository import RequestLogsRepository
from app.modules.usage.repository import AdditionalUsageRepository, UsageRepository
from tests.integration.test_http_responses_bridge import _FakeBridgeUpstreamWebSocket

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _repo_factory() -> AsyncIterator[ProxyRepositories]:
    async with SessionLocal() as session:
        yield ProxyRepositories(
            accounts=AccountsRepository(session),
            usage=UsageRepository(session),
            request_logs=RequestLogsRepository(session),
            sticky_sessions=StickySessionsRepository(session),
            api_keys=ApiKeysRepository(session),
            additional_usage=AdditionalUsageRepository(session),
        )


def _future_access_token(subject: str) -> str:
    # Synthetic, unsigned JWT in the upstream harness's header.body.sig format.
    # A future exp is essential: quarantine must not rely on access-token expiry.
    expires_at = int((utcnow() + timedelta(days=7)).replace(tzinfo=timezone.utc).timestamp())
    body = base64.urlsafe_b64encode(json.dumps({"sub": subject, "exp": expires_at}).encode()).rstrip(b"=")
    return f"header.{body.decode('ascii')}.sig"


async def _seed_account(account_id: str, *, used_percent: float = 0.0) -> Account:
    encryptor = TokenEncryptor()
    now = utcnow()
    account = Account(
        id=account_id,
        email=f"{account_id}@example.invalid",
        chatgpt_account_id=account_id,
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt(_future_access_token(account_id)),
        refresh_token_encrypted=encryptor.encrypt(f"refresh-{account_id}"),
        id_token_encrypted=encryptor.encrypt("synthetic-id-token"),
        last_refresh=now,
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    async with SessionLocal() as session:
        await AccountsRepository(session).upsert(account)
        usage = UsageRepository(session)
        now_epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
        for window, minutes in (("primary", 300), ("secondary", 10080)):
            await usage.add_entry(
                account_id=account_id,
                used_percent=used_percent,
                window=window,
                reset_at=now_epoch + minutes * 60,
                window_minutes=minutes,
                recorded_at=now,
            )
    return account


async def _get_account(account_id: str) -> Account:
    # A separate session proves the result was committed, not just mutated in memory.
    async with SessionLocal() as session:
        account = await AccountsRepository(session).get_by_id(account_id)
        assert account is not None
        return account


async def _assert_quarantined(balancer: LoadBalancer, failed_id: str, healthy_id: str) -> None:
    persisted = await _get_account(failed_id)
    assert persisted.status == AccountStatus.DEACTIVATED
    assert persisted.deactivation_reason

    # Do not let a random healthy pick hide that the failed account is still eligible.
    unavailable = await balancer.select_account(account_ids=[failed_id])
    assert unavailable.account is None
    selection = await balancer.select_account()
    assert selection.account is not None
    assert selection.account.id == healthy_id
    assert (await _get_account(failed_id)).status == AccountStatus.DEACTIVATED


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["invalid_grant", "refresh_token_reused", "token_expired"])
async def test_permanent_refresh_failure_quarantines_future_jwt_account(
    db_setup, monkeypatch, error_code: str
) -> None:
    failed = await _seed_account(f"quarantine-{error_code}")
    healthy = await _seed_account("quarantine-healthy", used_percent=50.0)
    balancer = LoadBalancer(_repo_factory)
    before = await balancer.select_account(account_ids=[failed.id])
    assert before.account is not None
    assert before.account.id == failed.id

    async def reject_refresh(refresh_token: str, **kwargs):
        raise RefreshError(error_code, "Upstream rejected the stored refresh credential", True)

    monkeypatch.setattr(auth_manager_module, "refresh_access_token", reject_refresh)
    async with SessionLocal() as session:
        manager = AuthManager(AccountsRepository(session))
        with pytest.raises(RefreshError) as failure:
            await manager.refresh_account(failed)
    assert failure.value.code == error_code
    assert failure.value.is_permanent
    # Check AuthManager's own persistence before any caller-level permanent mark.
    await _assert_quarantined(balancer, failed.id, healthy.id)


@pytest.mark.asyncio
async def test_permanent_request_auth_failure_quarantines_future_jwt_account(db_setup) -> None:
    failed = await _seed_account("quarantine-request-auth")
    healthy = await _seed_account("quarantine-request-healthy", used_percent=50.0)
    balancer = LoadBalancer(_repo_factory)
    before = await balancer.select_account(account_ids=[failed.id])
    assert before.account is not None
    assert before.account.id == failed.id

    # The non-refresh request failure entry point must enforce the same quarantine.
    await balancer.mark_permanent_failure(before.account, "account_auth_invalidated")
    await _assert_quarantined(balancer, failed.id, healthy.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code,transport_error",
    [("temporarily_unavailable", False), ("transport_error", True)],
)
async def test_transient_refresh_failure_preserves_credentials_and_selection(
    db_setup, monkeypatch, error_code: str, transport_error: bool
) -> None:
    account = await _seed_account("quarantine-transient")
    original_tokens = (
        account.access_token_encrypted,
        account.refresh_token_encrypted,
        account.id_token_encrypted,
    )

    async def reject_refresh(refresh_token: str, **kwargs):
        raise RefreshError(error_code, "Temporary upstream outage", False, transport_error=transport_error)

    monkeypatch.setattr(auth_manager_module, "refresh_access_token", reject_refresh)
    async with SessionLocal() as session:
        with pytest.raises(RefreshError) as failure:
            await AuthManager(AccountsRepository(session)).refresh_account(account)
    assert failure.value.code == error_code
    assert not failure.value.is_permanent
    persisted = await _get_account(account.id)
    assert persisted.status == AccountStatus.ACTIVE
    assert persisted.deactivation_reason is None
    assert persisted.last_refresh == account.last_refresh
    assert (
        persisted.access_token_encrypted,
        persisted.refresh_token_encrypted,
        persisted.id_token_encrypted,
    ) == original_tokens
    selection = await LoadBalancer(_repo_factory).select_account()
    assert selection.account is not None
    assert selection.account.id == account.id


@pytest.mark.asyncio
async def test_inflight_permanent_failure_adopts_concurrently_repaired_credentials(db_setup, monkeypatch) -> None:
    account = await _seed_account("quarantine-peer-repair")
    old_refresh = account.refresh_token_encrypted
    started = asyncio.Event()
    release = asyncio.Event()

    async def reject_old_refresh(refresh_token: str, **kwargs):
        started.set()
        await release.wait()
        raise RefreshError("invalid_grant", "The old credential was revoked", True)

    monkeypatch.setattr(auth_manager_module, "refresh_access_token", reject_old_refresh)
    encryptor = TokenEncryptor()
    repaired_access = _future_access_token("repaired-peer")
    repaired_tokens = (
        encryptor.encrypt(repaired_access),
        encryptor.encrypt("repaired-refresh"),
        encryptor.encrypt("repaired-id"),
    )
    async with SessionLocal() as session:
        manager = AuthManager(AccountsRepository(session))
        refreshing = asyncio.create_task(manager.refresh_account(account))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            # A real second DB session commits the repair while the first exchange
            # is pending. Only upstream I/O is controlled; the CAS/adoption is real.
            async with SessionLocal() as peer_session:
                assert await AccountsRepository(peer_session).rotate_tokens(
                    account.id,
                    *repaired_tokens,
                    utcnow(),
                    expected_refresh_token_encrypted=old_refresh,
                )
            release.set()
            adopted = await asyncio.wait_for(refreshing, timeout=5)
        finally:
            release.set()
            if not refreshing.done():
                refreshing.cancel()
            await asyncio.gather(refreshing, return_exceptions=True)

    assert adopted.status == AccountStatus.ACTIVE
    assert adopted.deactivation_reason is None
    assert encryptor.decrypt(adopted.access_token_encrypted) == repaired_access
    assert encryptor.decrypt(adopted.refresh_token_encrypted) == "repaired-refresh"
    assert encryptor.decrypt(adopted.id_token_encrypted) == "repaired-id"
    persisted = await _get_account(account.id)
    assert persisted.status == AccountStatus.ACTIVE
    assert persisted.deactivation_reason is None
    assert (
        persisted.access_token_encrypted,
        persisted.refresh_token_encrypted,
        persisted.id_token_encrypted,
    ) == repaired_tokens
    selection = await LoadBalancer(_repo_factory).select_account()
    assert selection.account is not None
    assert selection.account.id == account.id
    assert encryptor.decrypt(selection.account.refresh_token_encrypted) == "repaired-refresh"


class _HoldingUpstream(_FakeBridgeUpstreamWebSocket):
    """Leave the first accepted response pending until the test releases it."""

    async def send_text(self, text: str) -> None:
        if self.sent_text:
            # Pre-fix, a forbidden second dispatch must produce an observable
            # response rather than hang waiting for our pending first turn.
            await super().send_text(text)
            return
        self.sent_text.append(text)
        await self._messages.put(
            UpstreamWebSocketMessage(
                "text",
                text=json.dumps(
                    {
                        "type": "response.created",
                        "response": {"id": "resp_quarantine_1", "object": "response", "status": "in_progress"},
                    }
                ),
            )
        )

    async def finish_first(self) -> None:
        await self._messages.put(
            UpstreamWebSocketMessage(
                "text",
                text=json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_quarantine_1",
                            "object": "response",
                            "status": "completed",
                            "output": [],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        },
                    }
                ),
            )
        )


def _receive_terminal(websocket) -> dict:
    for _ in range(30):
        event = json.loads(websocket.receive_text())
        if event.get("type") in {"response.completed", "response.failed", "error"}:
            return event
    raise AssertionError("No terminal WebSocket event received")


def test_native_cached_socket_rejects_quarantined_new_turns_but_drains_accepted_turn(
    app_instance, monkeypatch
) -> None:
    upstreams: list[_HoldingUpstream] = []

    async def connect(*args, **kwargs):
        upstream = _HoldingUpstream("resp_quarantine")
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    # Loopback exercises real firewall/auth admission without bypassing either.
    with TestClient(app_instance, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.portal is not None
        account = client.portal.call(_seed_account, "quarantine-native")
        with client.websocket_connect("ws://localhost/backend-api/codex/responses") as websocket:
            payload = {"type": "response.create", "model": "gpt-5.4", "input": "first", "stream": True}
            websocket.send_json(payload)
            created = json.loads(websocket.receive_text())
            assert created["type"] == "response.created", created
            assert created["response"]["id"] == "resp_quarantine_1"
            assert len(upstreams) == 1

            client.portal.call(mark_account_routing_unavailable, account.id)
            websocket.send_json({**payload, "input": "forbidden while first is pending"})
            rejected = _receive_terminal(websocket)
            assert rejected["type"] in {"response.failed", "error"}, rejected
            error = rejected.get("error") or rejected.get("response", {}).get("error", {})
            assert error.get("code") == "account_deactivated", rejected
            assert len(upstreams) == 1
            assert len(upstreams[0].sent_text) == 1
            assert not upstreams[0].closed

            client.portal.call(upstreams[0].finish_first)
            completed = _receive_terminal(websocket)
            assert completed["type"] == "response.completed", completed
            assert completed["response"]["id"] == "resp_quarantine_1"
            assert len(upstreams[0].sent_text) == 1

            # The now-idle cached socket is still quarantined; completing an
            # accepted response must not accidentally reopen it to new work.
            websocket.send_json({**payload, "input": "forbidden after drain"})
            rejected_idle = _receive_terminal(websocket)
            assert rejected_idle["type"] in {"response.failed", "error"}, rejected_idle
            assert len(upstreams) == 1
            assert len(upstreams[0].sent_text) == 1
