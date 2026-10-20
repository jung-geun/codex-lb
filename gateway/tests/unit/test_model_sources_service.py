from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from app.core.crypto import TokenEncryptor
from app.db.models import ModelSource, ModelSourceQuotaObservation
from app.modules.model_sources.repository import ModelSourcesRepository
from app.modules.model_sources.schemas import ModelSourceCreateRequest, ModelSourceModelInput, ModelSourceUpdateRequest
from app.modules.model_sources.service import ModelSourcesService, ModelSourceValidationError


class _FakeEncryptor:
    def encrypt(self, token: str) -> bytes:
        return f"encrypted:{token}".encode()


class _FakeRepository:
    def __init__(self) -> None:
        self.created: ModelSource | None = None
        self.committed = False
        self.rolled_back = False
        self.quota_history: dict[str, list[ModelSourceQuotaObservation]] = {}

    async def create(self, row: ModelSource, *, commit: bool = True) -> ModelSource:
        _stamp_source(row)
        self.created = row
        return row

    async def get_by_id(self, source_id: str) -> ModelSource | None:
        if self.created is None or self.created.id != source_id:
            return None
        return self.created

    async def quota_history_component_ids(self, source_id: str) -> set[str]:
        return {source_id} if self.created is not None and self.created.id == source_id else set()

    async def clear_quota_history(self, source_ids: set[str]) -> None:
        for source_id in source_ids:
            self.quota_history.pop(source_id, None)

    async def replace_models(self, source: ModelSource, models, *, commit: bool = True) -> None:
        source.models = models
        _stamp_source(source)

    async def refresh_models(self, source: ModelSource) -> None:
        _stamp_source(source)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _stamp_source(row: ModelSource) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    row.created_at = row.created_at or now
    row.updated_at = now
    for index, model in enumerate(row.models, start=1):
        model.id = model.id or index
        model.source_id = row.id
        model.created_at = model.created_at or now
        model.updated_at = now


def _as_repository(repository: _FakeRepository) -> ModelSourcesRepository:
    return cast(ModelSourcesRepository, repository)


def _fake_encryptor() -> TokenEncryptor:
    return cast(TokenEncryptor, _FakeEncryptor())


@pytest.mark.asyncio
async def test_create_source_validates_absolute_http_url() -> None:
    service = ModelSourcesService(_as_repository(_FakeRepository()), encryptor=_fake_encryptor())

    with pytest.raises(ModelSourceValidationError, match="absolute HTTP"):
        await service.create_source(
            ModelSourceCreateRequest(
                name="Local",
                base_url="localhost:8000/v1",
                models=[ModelSourceModelInput(model="local-coder")],
            )
        )


@pytest.mark.asyncio
async def test_create_source_rejects_duplicate_models() -> None:
    service = ModelSourcesService(_as_repository(_FakeRepository()), encryptor=_fake_encryptor())

    with pytest.raises(ModelSourceValidationError, match="Duplicate"):
        await service.create_source(
            ModelSourceCreateRequest(
                name="Local",
                base_url="http://localhost:8000/v1",
                models=[
                    ModelSourceModelInput(model="local-coder"),
                    ModelSourceModelInput(model="local-coder"),
                ],
            )
        )


@pytest.mark.asyncio
async def test_create_source_encrypts_api_key_without_account_row() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())

    created = await service.create_source(
        ModelSourceCreateRequest(
            name="Local",
            base_url="http://localhost:8000/v1/",
            api_key="secret",
            models=[ModelSourceModelInput(model="local-coder")],
        )
    )

    assert repo.created is not None
    assert repo.created.api_key_encrypted == b"encrypted:secret"
    assert repo.created.base_url == "http://localhost:8000/v1"
    assert repo.created.supports_audio_transcriptions is False
    assert repo.created.models[0].model == "local-coder"
    assert created.id.startswith("src_")


@pytest.mark.asyncio
async def test_create_source_persists_audio_transcription_capability() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())

    created = await service.create_source(
        ModelSourceCreateRequest(
            name="ASR",
            base_url="http://localhost:8000/v1/",
            supports_audio_transcriptions=True,
            models=[ModelSourceModelInput(model="whisper-large-v3")],
        )
    )

    assert repo.created is not None
    assert repo.created.supports_audio_transcriptions is True
    assert created.supports_audio_transcriptions is True


@pytest.mark.asyncio
async def test_update_source_clears_nullable_fields_when_null_is_sent() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    created = await service.create_source(
        ModelSourceCreateRequest(
            name="Local",
            base_url="http://localhost:8000/v1/",
            api_key="secret",
            timeout_seconds=30,
            max_concurrency=2,
            models=[ModelSourceModelInput(model="local-coder")],
        )
    )

    updated = await service.update_source(
        created.id,
        ModelSourceUpdateRequest(api_key=None, timeout_seconds=None, max_concurrency=None),
    )

    assert repo.created is not None
    assert repo.created.api_key_encrypted is None
    assert repo.created.timeout_seconds is None
    assert repo.created.max_concurrency is None
    assert updated.timeout_seconds is None
    assert updated.max_concurrency is None


@pytest.mark.asyncio
async def test_clients_cannot_self_verify_subscription_coverage() -> None:
    service = ModelSourcesService(_as_repository(_FakeRepository()), encryptor=_fake_encryptor())
    with pytest.raises(ModelSourceValidationError, match="provider validation"):
        await service.create_source(ModelSourceCreateRequest(
            name="custom", base_url="http://localhost/v1",
            models=[ModelSourceModelInput(model="model", subscription_eligible=True)],
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    ModelSourceUpdateRequest(api_key="replacement"),
    ModelSourceUpdateRequest(base_url="https://different.example/v1"),
    ModelSourceUpdateRequest(messages_base_url="https://different.example/messages"),
    ModelSourceUpdateRequest(models=[ModelSourceModelInput(model="new-model")]),
    ModelSourceUpdateRequest(supports_messages=True),
    ModelSourceUpdateRequest(billing_mode="subscription"),
    ModelSourceUpdateRequest(quota_group="shared-subscription"),
    ModelSourceUpdateRequest(key_type="standard"),
])
async def test_sensitive_source_changes_invalidate_verified_entitlement(payload) -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    created = await service.create_source(ModelSourceCreateRequest(
        name="local", base_url="http://localhost/v1", models=[ModelSourceModelInput(model="old-model")],
    ))
    assert repo.created is not None
    repo.created.entitlement_status = "verified"
    repo.created.entitlement_verified_at = datetime.now(UTC)
    repo.created.extra_usage_enabled = False
    repo.created.detected_key_type = "personal"
    repo.created.models[0].subscription_eligible = True
    updated = await service.update_source(created.id, payload)
    assert updated.entitlement_status == "unverified"
    assert updated.entitlement_verified_at is None
    assert updated.extra_usage_enabled is None
    assert updated.quota_checked_at is None
    assert updated.detected_key_type == "unknown"
    assert updated.quota_windows == []
    assert all(not model.subscription_eligible for model in updated.models)


@pytest.mark.asyncio
async def test_cosmetic_source_update_preserves_verification() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    created = await service.create_source(ModelSourceCreateRequest(name="local", base_url="http://localhost/v1"))
    assert repo.created is not None
    repo.created.entitlement_status = "verified"
    repo.created.entitlement_verified_at = datetime.now(UTC)
    updated = await service.update_source(created.id, ModelSourceUpdateRequest(name="renamed", priority=7))
    assert updated.name == "renamed"
    assert updated.priority == 7
    assert updated.entitlement_status == "verified"


@pytest.mark.asyncio
async def test_model_subscription_restriction_is_operator_owned() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    await service.create_source(ModelSourceCreateRequest(name="local", base_url="http://localhost/v1", models=[
        ModelSourceModelInput(model="unrestricted"),
        ModelSourceModelInput(model="restricted", subscription_eligible=False),
    ]))
    assert repo.created is not None
    assert [row.subscription_restricted for row in repo.created.models] == [False, True]


@pytest.mark.asyncio
async def test_provider_preset_applies_server_defaults_without_claiming_entitlement() -> None:
    service = ModelSourcesService(_as_repository(_FakeRepository()), encryptor=_fake_encryptor())
    row = await service.create_source(ModelSourceCreateRequest(name="Claude", provider="claude", base_url="https://api.anthropic.com"))
    assert row.auth_type == "claude_oauth"
    assert row.billing_mode == "subscription"
    assert row.supports_messages and not row.supports_chat_completions
    assert row.entitlement_status == "unverified"
    assert row.quota_windows == []
    assert row.models == []
    assert row.credential_expires_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"base_url": "https://api.anthropic.com.attacker.example"},
    {"base_url": "https://api.anthropic.com/redirect"},
    {"messages_base_url": "https://other.example"},
    {"auth_type": "api_key"}, {"billing_mode": "api_metered"}, {"supports_responses": True},
])
async def test_provider_preset_rejects_unvalidated_endpoints_auth_and_capabilities(changes) -> None:
    service = ModelSourcesService(_as_repository(_FakeRepository()), encryptor=_fake_encryptor())
    fields = {"name": "Claude", "provider": "claude", "base_url": "https://api.anthropic.com", **changes}
    with pytest.raises(ModelSourceValidationError):
        await service.create_source(ModelSourceCreateRequest(**fields))


@pytest.mark.asyncio
async def test_manual_quota_group_is_explicit_and_preserved_when_replacing_key() -> None:
    service = ModelSourcesService(_as_repository(_FakeRepository()), encryptor=_fake_encryptor())
    created = await service.create_source(ModelSourceCreateRequest(
        name="grouped", base_url="http://localhost/v1", quota_group="team:one",
    ))
    assert created.quota_group == "team:one"
    changed = await service.update_source(created.id, ModelSourceUpdateRequest(api_key="new", quota_group="team:one"))
    assert changed.quota_group == "team:one"
    assert changed.entitlement_status == "unverified"


@pytest.mark.asyncio
async def test_source_controls_preserve_verified_subscription_and_model_coverage() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    created = await service.create_source(ModelSourceCreateRequest(
        name="Claude", provider="claude", base_url="https://api.anthropic.com",
        models=[ModelSourceModelInput(model="claude-sonnet-4-6")],
    ))
    assert repo.created is not None
    repo.created.entitlement_status = "verified"
    repo.created.entitlement_verified_at = datetime(2026, 10, 19, 12)
    repo.created.extra_usage_enabled = False
    repo.created.models[0].subscription_eligible = True
    updated = await service.update_source(created.id, ModelSourceUpdateRequest(
        routing_policy="burn_first", limit_warmup_enabled=True, is_enabled=False,
    ))
    assert updated.routing_policy == "burn_first" and updated.limit_warmup_enabled is True
    assert updated.is_enabled is False
    assert updated.entitlement_status == "verified"
    assert updated.entitlement_verified_at == datetime(2026, 10, 19, 12)
    assert updated.extra_usage_enabled is False
    assert updated.models[0].subscription_eligible is True
    assert repo.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["normal", "burn_first", "preserve"])
async def test_routing_policy_is_available_without_synthetic_metered_warmup(policy) -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    created = await service.create_source(ModelSourceCreateRequest(
        name="Metered", provider="openai", base_url="https://api.openai.com/v1", api_key="synthetic-key",
    ))
    updated = await service.update_source(created.id, ModelSourceUpdateRequest(routing_policy=policy))
    assert updated.routing_policy == policy
    assert updated.warmup_supported is False
    assert updated.limit_warmup_enabled is False
    assert updated.limit_warmup is None


@pytest.mark.asyncio
async def test_opted_in_source_must_disable_warmup_when_becoming_metered() -> None:
    repo = _FakeRepository()
    service = ModelSourcesService(_as_repository(repo), encryptor=_fake_encryptor())
    created = await service.create_source(ModelSourceCreateRequest(
        name="Claude", provider="claude", base_url="https://api.anthropic.com",
    ))
    await service.update_source(created.id, ModelSourceUpdateRequest(limit_warmup_enabled=True))
    with pytest.raises(ModelSourceValidationError, match="fixed Claude OAuth subscriptions"):
        await service.update_source(created.id, ModelSourceUpdateRequest(provider="anthropic", api_key="metered-key"))
    updated = await service.update_source(created.id, ModelSourceUpdateRequest(
        provider="anthropic", api_key="metered-key", limit_warmup_enabled=False,
    ))
    assert updated.billing_mode == "api_metered"
    assert updated.limit_warmup_enabled is False
    assert updated.warmup_supported is False
