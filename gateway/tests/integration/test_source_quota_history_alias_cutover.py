"""Quota history cannot survive an alias-component identity cutover."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import TokenEncryptor
from app.db.models import (
    ModelSource,
    ModelSourceModel,
    ModelSourceOAuthCredential,
    ModelSourceQuotaObservation,
    ModelSourceQuotaWindow,
)
from app.db.session import SessionLocal
from app.modules.model_sources import subscription_runtime as runtime
from app.modules.model_sources.provider_schemas import configuration_fingerprint, presets
from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import CatalogModel, ProviderSnapshot, QuotaWindow
from app.modules.model_sources.repository import ModelSourcesRepository

pytestmark = pytest.mark.integration
MODEL = "claude-alias-history-entitled"
OLD = {"a", "b", "c", "d"}
NEW = {"e", "f", "g", "h"}
ALL = OLD | NEW | {"outside"}


@pytest.fixture
async def alias_clock(db_setup, monkeypatch):
    @asynccontextmanager
    async def background():
        async with SessionLocal() as session:
            yield session

    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    monkeypatch.setattr(runtime, "get_background_session", background)
    monkeypatch.setattr(runtime, "utcnow", lambda: now)
    return now


async def seed_graph(now, *, root_provider="claude"):
    # Alternating edge types force a transitive traversal:
    # a --group--> b --principal--> c --group--> d
    # e --group--> f --principal--> g --group--> h
    specs = {
        "a": ("old-root", "old"),
        "b": ("old-bridge", "old"),
        "c": ("old-bridge", "old-tail"),
        "d": ("old-tail-principal", "old-tail"),
        "e": ("new-root", "new"),
        "f": ("new-bridge", "new"),
        "g": ("new-bridge", "new-tail"),
        "h": ("new-tail-principal", "new-tail"),
        "outside": ("outside", "outside"),
    }
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        observations = []
        for source_id, (account, group) in specs.items():
            provider = root_provider if source_id == "a" else "claude"
            preset = next(preset for preset in presets() if preset.provider == provider)
            source = ModelSource(
                id=source_id, name=source_id, provider=provider, auth_type=preset.auth_type,
                kind="anthropic_compatible", base_url=preset.base_url, messages_base_url=preset.messages_base_url,
                api_key_encrypted=encryptor.encrypt("key-" + source_id) if preset.auth_type == "api_key" else None,
                billing_mode="subscription", quota_group=group, routing_policy="normal",
                limit_warmup_enabled=False, is_enabled=source_id not in {"d", "g"},
                supports_messages=preset.supports_messages, supports_count_tokens=preset.supports_count_tokens,
                supports_chat_completions=preset.supports_chat_completions,
                supports_responses=preset.supports_responses, supports_embeddings=preset.supports_embeddings,
                supports_audio_transcriptions=preset.supports_audio_transcriptions,
                entitlement_status="verified", entitlement_verified_at=now, extra_usage_enabled=False,
                quota_checked_at=now,
                models=[ModelSourceModel(model=MODEL, subscription_eligible=True, subscription_restricted=False)],
                quota_windows=[ModelSourceQuotaWindow(
                    window_key="five_hour", used_ratio=0.1, observed_at=now, resets_at=now + timedelta(hours=2),
                )],
            )
            if preset.auth_type == "claude_oauth":
                source.oauth_credential = ModelSourceOAuthCredential(
                    access_token_encrypted=encryptor.encrypt("access-" + source_id),
                    refresh_token_encrypted=encryptor.encrypt("refresh-" + source_id),
                    expires_at=now + timedelta(hours=1), generation=0,
                    account_uuid=account, organization_uuid="organization",
                )
            session.add(source)
            observations.append(ModelSourceQuotaObservation(
                source_id=source_id, window_key="five_hour", used_ratio=0.1,
                observed_at=now - timedelta(minutes=5), resets_at=now + timedelta(hours=2),
            ))
        await session.flush()
        session.add_all(observations)
        await session.commit()


async def history_source_ids():
    async with SessionLocal() as session:
        return set((await session.scalars(select(ModelSourceQuotaObservation.source_id))).all())


def snapshot(now, *, account="new-root", group="new"):
    return ProviderSnapshot(
        billing_mode="subscription", entitlement_status="verified", extra_usage_enabled=False,
        account_uuid=account, organization_uuid="organization", quota_group=group,
        windows=[
            QuotaWindow("five_hour", used_ratio=0.2, resets_at=now + timedelta(hours=2)),
            QuotaWindow("seven_day", used_ratio=0.3, resets_at=now + timedelta(days=2)),
        ],
        models=[CatalogModel(model=MODEL, subscription_eligible=True)],
    )


@pytest.mark.asyncio
async def test_alias_component_uses_transitive_principal_or_group_edges(alias_clock):
    await seed_graph(alias_clock)
    async with SessionLocal() as session:
        repository = ModelSourcesRepository(session)
        assert await repository.quota_history_component_ids("a") == OLD
        assert await repository.quota_history_component_ids("e") == NEW
        assert await repository.quota_history_component_ids("outside") == {"outside"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cutover", ["quota_group", "credential", "identity"])
async def test_management_cutover_clears_union_of_old_and_new_alias_components(alias_clock, async_client, cutover):
    await seed_graph(alias_clock, root_provider="kimi" if cutover == "credential" else "claude")
    payload = {"quotaGroup": "new"}
    if cutover == "credential":
        payload["apiKey"] = "replacement-native-key"
    elif cutover == "identity":
        payload.update(
            provider="custom", baseUrl="https://new-provider.example/v1", authType="api_key", apiKey="replacement",
        )
    response = await async_client.patch("/api/model-sources/a", json=payload)
    assert response.status_code == 200, response.text
    # Old peers are disconnected from a now, so invalidating only the new graph
    # would leave b/c/d contaminated. A one-hop clear also misses c/d and g/h.
    assert await history_source_ids() == {"outside"}
    async with SessionLocal() as session:
        source = await session.get(ModelSource, "a")
        assert source.entitlement_status == "unverified" and source.quota_windows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"routingPolicy": "burn_first"}, {"isEnabled": False}, {"limitWarmupEnabled": True},
])
@pytest.mark.parametrize("legacy_message_root", [False, True])
async def test_control_only_patch_preserves_every_alias_history(
    alias_clock, async_client, payload, legacy_message_root,
):
    await seed_graph(alias_clock)
    if legacy_message_root:
        async with SessionLocal() as session:
            source = await session.get(ModelSource, "a")
            source.messages_base_url = None  # Native transport falls back to the same fixed base_url.
            await session.commit()
    response = await async_client.patch("/api/model-sources/a", json=payload)
    assert response.status_code == 200, response.text
    assert await history_source_ids() == ALL
    async with SessionLocal() as session:
        source = await session.get(ModelSource, "a")
        assert source.entitlement_status == "verified" and source.quota_group == "old"
        assert source.quota_windows[0].used_ratio == 0.1
        assert source.oauth_credential.account_uuid == "old-root"
        assert source.oauth_credential.generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["old-root", "new-root"])
async def test_oauth_reauthorization_discards_old_alias_history_even_for_same_account(alias_clock, account):
    await seed_graph(alias_clock)
    original = await runtime.load_source("a")
    await runtime.install_credential(
        "a", claude.TokenPair(access_token="replacement-access", refresh_token="replacement-refresh", expires_in=3600),
        expected_generation=0, expected_configuration=configuration_fingerprint(original),
    )
    assert await history_source_ids() == NEW | {"outside"}
    await runtime._persist_snapshot(
        await runtime.load_source("a"),
        snapshot(alias_clock, account=account, group="old" if account == "old-root" else "new"),
    )
    expected = NEW | {"a", "outside"} if account == "old-root" else {"a", "outside"}
    assert await history_source_ids() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["principal", "group", "both"])
async def test_native_cutover_clears_alias_union_but_retains_new_observed_baseline(alias_clock, change):
    await seed_graph(alias_clock)
    account = "new-root" if change in {"principal", "both"} else "old-root"
    group = "new" if change in {"group", "both"} else "old"
    await runtime._persist_snapshot(await runtime.load_source("a"), snapshot(alias_clock, account=account, group=group))
    assert await history_source_ids() == {"a", "outside"}
    async with SessionLocal() as session:
        fresh = list((await session.scalars(select(ModelSourceQuotaObservation).where(
            ModelSourceQuotaObservation.source_id == "a",
        ))).all())
        assert len(fresh) == 2 and all(sample.observed_at == alias_clock for sample in fresh)
        assert {sample.used_ratio for sample in fresh} == {0.2, 0.3}


@pytest.mark.asyncio
async def test_same_native_principal_and_group_refresh_preserves_alias_history(alias_clock):
    await seed_graph(alias_clock)
    await runtime._persist_snapshot(
        await runtime.load_source("a"), snapshot(alias_clock, account="old-root", group="old"),
    )
    assert await history_source_ids() == ALL
    async with SessionLocal() as session:
        own_samples = list((await session.scalars(select(ModelSourceQuotaObservation).where(
            ModelSourceQuotaObservation.source_id == "a",
        ))).all())
        assert len(own_samples) == 3
