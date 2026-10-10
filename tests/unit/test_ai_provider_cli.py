from __future__ import annotations

from typer.testing import CliRunner

from xianyu_agent.application.ai.provider_service import AIProviderView
from xianyu_agent.cli.commands import ai as ai_cmd
from xianyu_agent.cli.main import app


class FakeProviderService:
    def __init__(self) -> None:
        self.enabled: list[tuple[str, bool]] = []
        self.rotated: list[str] = []
        self.retries: list[tuple[str, int]] = []
        self.created: list[dict[str, object]] = []

    async def list(self) -> list[AIProviderView]:
        return [
            AIProviderView(
                id=1,
                name="test-provider",
                provider_type="openai_compatible",
                base_url="https://example.invalid/v1",
                default_model="test-model",
                enabled=False,
                timeout_seconds=15,
                max_retries=0,
                configured=True,
            )
        ]

    async def set_enabled(self, name: str, enabled: bool) -> AIProviderView:
        self.enabled.append((name, enabled))
        return (await self.list())[0]

    async def replace_api_key(self, name: str, _api_key: object) -> AIProviderView:
        self.rotated.append(name)
        return (await self.list())[0]

    async def set_max_retries(self, name: str, max_retries: int) -> AIProviderView:
        self.retries.append((name, max_retries))
        return (await self.list())[0]

    async def create(self, **kwargs: object) -> AIProviderView:
        self.created.append(kwargs)
        return (await self.list())[0]


def test_ai_cli_lists_secret_free_provider_view(monkeypatch) -> None:
    service = FakeProviderService()

    def fake_service() -> FakeProviderService:
        return service

    monkeypatch.setattr(ai_cmd, "_service", fake_service)

    result = CliRunner().invoke(app, ["ai", "list"])

    assert result.exit_code == 0
    assert "test-provider" in result.output
    assert "https://example.invalid" not in result.output


def test_ai_cli_enable_disable_and_rotate_key(monkeypatch) -> None:
    service = FakeProviderService()

    def fake_service() -> FakeProviderService:
        return service

    monkeypatch.setattr(ai_cmd, "_service", fake_service)
    runner = CliRunner()

    enabled = runner.invoke(app, ["ai", "enable", "--name", "test-provider"])
    disabled = runner.invoke(app, ["ai", "disable", "--name", "test-provider"])
    rotated = runner.invoke(
        app,
        ["ai", "rotate-key", "--name", "test-provider", "--api-key", "test-key"],
    )
    retries = runner.invoke(
        app, ["ai", "set-retries", "--name", "test-provider", "--max-retries", "1"]
    )

    assert enabled.exit_code == disabled.exit_code == rotated.exit_code == retries.exit_code == 0
    assert service.enabled == [("test-provider", True), ("test-provider", False)]
    assert service.rotated == ["test-provider"]
    assert service.retries == [("test-provider", 1)]
    assert "test-key" not in rotated.output


def test_ai_cli_adds_an_explicitly_typed_provider(monkeypatch) -> None:
    service = FakeProviderService()

    def fake_service() -> FakeProviderService:
        return service

    monkeypatch.setattr(ai_cmd, "_service", fake_service)

    result = CliRunner().invoke(
        app,
        [
            "ai",
            "add",
            "--type",
            "azure_openai",
            "--name",
            "azure",
            "--model",
            "gpt-4o",
            "--base-url",
            "https://resource.openai.azure.com",
            "--api-key",
            "secret-key",
            "--extra",
            "deployment=deploy-1",
        ],
    )

    assert result.exit_code == 0
    created = service.created[0]
    assert created["provider_type"] == "azure_openai"
    assert created["extra_config"] == {"deployment": "deploy-1"}
    assert "secret-key" not in result.output


def test_ai_cli_rejects_a_malformed_extra_option(monkeypatch) -> None:
    service = FakeProviderService()

    def fake_service() -> FakeProviderService:
        return service

    monkeypatch.setattr(ai_cmd, "_service", fake_service)

    result = CliRunner().invoke(
        app,
        [
            "ai",
            "add",
            "--type",
            "anthropic",
            "--name",
            "claude",
            "--model",
            "claude-3",
            "--api-key",
            "secret-key",
            "--extra",
            "deployment",
        ],
    )

    assert result.exit_code != 0
    assert service.created == []
