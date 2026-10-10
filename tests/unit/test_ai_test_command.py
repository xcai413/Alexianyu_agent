"""`ai test`:真调 Provider,但绝不外发、不落库,并如实报告安全闸判定。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from xianyu_agent.cli.commands import ai as ai_cli
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain.ai import AIRequest, AIResponse, UnconfiguredError

runner = CliRunner()


@pytest.fixture
def ai_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "ai.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    yield tmp_path
    reset_settings_cache()


class _FakeProvider:
    def __init__(self, text: str = "在的,9.9 包邮", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[AIRequest] = []

    async def generate(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return AIResponse(text=self.text, provider="openai_compatible", model="test-model")


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr(ai_cli, "VaultBackedProvider", lambda *_, **__: provider)


def test_ai_test_prints_draft_without_sending(
    ai_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider()
    _patch_provider(monkeypatch, provider)

    result = runner.invoke(app, ["ai", "test", "--message", "还在吗"])

    assert result.exit_code == 0, result.output
    assert "在的,9.9 包邮" in result.output
    assert "openai_compatible" in result.output
    assert "通过输出安全闸" in result.output
    assert provider.requests[0].buyer_message == "还在吗"


def test_ai_test_reports_a_blocked_draft(
    ai_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型想留站外联系方式时,命令要如实说这句话发不出去。"""
    provider = _FakeProvider(text="加我微信 abc12345 便宜点")
    _patch_provider(monkeypatch, provider)

    result = runner.invoke(app, ["ai", "test", "--message", "便宜点"])

    assert result.exit_code == 1
    assert "会被输出安全闸拦截" in result.output
    assert "站外联系方式" in result.output


def test_ai_test_reports_unconfigured_provider(
    ai_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider(error=UnconfiguredError("AI Provider 未配置或密钥不可用"))
    _patch_provider(monkeypatch, provider)

    result = runner.invoke(app, ["ai", "test", "--message", "在吗"])

    assert result.exit_code == 1
    assert "未配置或密钥不可用" in result.output


def test_ai_test_never_leaks_exception_detail(
    ai_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider 抛出的正文可能带请求细节,输出只能给类型。"""
    provider = _FakeProvider(error=RuntimeError("api_key=sk-should-not-appear"))
    _patch_provider(monkeypatch, provider)

    result = runner.invoke(app, ["ai", "test", "--message", "在吗"])

    assert result.exit_code == 1
    assert "RuntimeError" in result.output
    assert "sk-should-not-appear" not in result.output
