from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.application.ai import AIProviderService
from xianyu_agent.application.ports.secret_vault import SecretMetadata, SecretRef, SecretValue
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod


class FakeVault:
    def __init__(self) -> None:
        self.values: dict[str, SecretValue] = {}

    async def put(
        self, secret: SecretValue, *, metadata: SecretMetadata | None = None
    ) -> SecretRef:
        assert metadata is not None
        ref = SecretRef("sv1_test")
        self.values[ref.value] = secret
        return ref

    async def configured(self, ref: SecretRef) -> bool:
        return ref.value in self.values

    async def replace(self, ref: SecretRef, secret: SecretValue) -> SecretRef:
        self.values[ref.value] = secret
        return ref

    async def resolve(self, ref: SecretRef) -> SecretValue:
        return self.values[ref.value]


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "ai.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_create_provider_returns_secret_free_view(clean_db) -> None:
    vault = FakeVault()
    service = AIProviderService(vault)  # type: ignore[arg-type]
    view = await service.create(
        provider_type="openai_compatible",
        name="test",
        base_url="https://example.invalid/v1",
        default_model="test-model",
        api_key=SecretValue("key"),
    )

    assert view.configured is True
    assert not hasattr(view, "secret_ref")
    assert "key" not in repr(view)
    assert (await service.list()) == [view]


@pytest.mark.asyncio
async def test_provider_enable_and_secret_rotation(clean_db) -> None:
    vault = FakeVault()
    service = AIProviderService(vault)  # type: ignore[arg-type]
    await service.create(
        provider_type="openai_compatible",
        name="test",
        base_url="https://example.invalid/v1",
        default_model="test-model",
        api_key=SecretValue("old"),
    )

    enabled = await service.set_enabled("test", True)
    rotated = await service.replace_api_key("test", SecretValue("new"))

    assert enabled.enabled is True
    assert rotated.configured is True
    assert vault.values["sv1_test"].reveal() == "new"


@pytest.mark.asyncio
async def test_provider_retry_limit_is_bounded(clean_db) -> None:
    service = AIProviderService(FakeVault())  # type: ignore[arg-type]
    await service.create(
        provider_type="openai_compatible",
        name="test",
        base_url="https://example.invalid/v1",
        default_model="test-model",
    )

    view = await service.set_max_retries("test", 2)

    assert view.max_retries == 2
    with pytest.raises(ValueError, match="0 到 3"):
        await service.set_max_retries("test", 4)


@pytest.mark.asyncio
async def test_create_rejects_an_undeclared_provider_type(clean_db) -> None:
    service = AIProviderService(FakeVault())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="未知 AI Provider 类型"):
        await service.create(
            provider_type="gpt-4o",
            name="guess",
            base_url="https://example.invalid/v1",
            default_model="gpt-4o",
        )


@pytest.mark.asyncio
async def test_resolve_enabled_returns_the_stored_type_and_extra_config(clean_db) -> None:
    vault = FakeVault()
    service = AIProviderService(vault)  # type: ignore[arg-type]
    await service.create(
        provider_type="azure_openai",
        name="azure",
        base_url="https://resource.openai.azure.com",
        default_model="gpt-4o",
        api_key=SecretValue("key"),
        extra_config={"deployment": "deploy-1", "api_version": "2024-06-01"},
    )
    await service.set_enabled("azure", True)

    resolved = await service.resolve_enabled()

    assert resolved.provider_type == "azure_openai"
    assert resolved.extra_config["deployment"] == "deploy-1"
    assert resolved.api_key.reveal() == "key"


@pytest.mark.asyncio
async def test_resolve_enabled_can_be_limited_to_one_type(clean_db) -> None:
    service = AIProviderService(FakeVault())  # type: ignore[arg-type]
    await service.create(
        provider_type="openai_chat",
        name="chat",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
        api_key=SecretValue("key"),
        enabled=True,
    )

    assert (await service.resolve_enabled("openai_chat")).provider_type == "openai_chat"
    with pytest.raises(ValueError, match="未配置"):
        await service.resolve_enabled("anthropic")
