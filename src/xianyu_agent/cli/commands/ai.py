"""AI Provider configuration commands; never print secret material."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from typing import TypeVar

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.ai import AIProviderService
from xianyu_agent.application.ai.context_service import ContextService
from xianyu_agent.application.ports.secret_vault import SecretValue
from xianyu_agent.config import get_settings
from xianyu_agent.domain.ai import (
    AIConversationContext,
    AIRequest,
    SafetyPolicy,
    UnconfiguredError,
)
from xianyu_agent.infrastructure.ai.vault_provider import VaultBackedProvider
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault

app = typer.Typer(help="AI Provider 配置(密钥仅保存到 SecretVault)。")
console = Console()
Result = TypeVar("Result")


def _parse_extra(values: Iterable[str]) -> dict[str, str]:
    """Parse repeated key=value options into explicit provider configuration."""
    extra: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter(f"--extra 需要 key=value 形式: {item}")
        extra[key.strip()] = value.strip()
    return extra


def _service() -> AIProviderService:
    return AIProviderService(SqlAlchemySecretVault(FernetKeyring.from_environment()))


def _run(operation: Coroutine[object, object, Result]) -> Result:
    """Run a command without exposing exception detail that could contain secrets."""
    try:
        return asyncio.run(operation)
    except Exception as exc:
        console.print(f"[red]AI Provider 配置失败: {type(exc).__name__}。[/red]")
        raise typer.Exit(code=1) from exc


@app.command("list")
def list_providers() -> None:
    async def run() -> None:
        rows = await _service().list()
        table = Table(title="AI Providers")
        for column in ("name", "type", "model", "enabled", "configured"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                row.name,
                row.provider_type,
                row.default_model,
                "Y" if row.enabled else "N",
                "Y" if row.configured else "N",
            )
        console.print(table)

    _run(run())


@app.command("add")
def add_provider(  # noqa: PLR0917 - Typer 命令参数本身就是关键字参数
    provider_type: str = typer.Option(
        ..., "--type", help="Provider 类型,必须是显式配置而非从模型名推断"
    ),
    name: str = typer.Option(...),
    model: str = typer.Option(...),
    base_url: str = typer.Option(
        "", "--base-url", help="接口地址;仅 openai_chat 留空时使用官方默认端点"
    ),
    api_key: str = typer.Option(..., prompt=True, hide_input=True),
    extra: list[str] | None = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        None, "--extra", help="额外配置 key=value,可重复(例如 azure_openai 的 deployment)"
    ),
) -> None:
    async def run() -> None:
        view = await _service().create(
            provider_type=provider_type,
            name=name,
            base_url=base_url,
            default_model=model,
            api_key=SecretValue(api_key),
            extra_config=_parse_extra(extra or []),
        )
        console.print(
            f"[green]OK[/green] Provider {view.name}({view.provider_type}) "
            "已创建(configured=Y, enabled=N)"
        )

    _run(run())


@app.command("add-compatible")
def add_compatible(
    name: str = typer.Option(...),
    base_url: str = typer.Option(...),
    model: str = typer.Option(...),
    api_key: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    """等价于 ai add --type openai_compatible。"""

    async def run() -> None:
        view = await _service().create(
            provider_type="openai_compatible",
            name=name,
            base_url=base_url,
            default_model=model,
            api_key=SecretValue(api_key),
        )
        console.print(f"[green]OK[/green] Provider {view.name} 已创建(configured=Y, enabled=N)")

    _run(run())


@app.command("enable")
def enable(name: str = typer.Option(...)) -> None:
    _run(_service().set_enabled(name, True))
    console.print("[green]OK[/green] Provider 已启用。")


@app.command("disable")
def disable(name: str = typer.Option(...)) -> None:
    _run(_service().set_enabled(name, False))
    console.print("[green]OK[/green] Provider 已停用。")


@app.command("set-retries")
def set_retries(
    name: str = typer.Option(...),
    max_retries: int = typer.Option(..., min=0, max=3),
) -> None:
    """设置 AI 文本生成可重试次数, 不会重试闲鱼消息发送。"""
    _run(_service().set_max_retries(name, max_retries))
    console.print("[green]OK[/green] Provider 重试次数已更新。")


@app.command("rotate-key")
def rotate_key(
    name: str = typer.Option(...),
    api_key: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    """替换既有 Provider 的 API Key, 终端不会回显密钥。"""
    _run(_service().replace_api_key(name, SecretValue(api_key)))
    console.print("[green]OK[/green] Provider 密钥已轮换。")


@app.command("test")
def run_ai_test(
    message: str = typer.Option(..., "--message", "-m", help="模拟买家说的话。"),
    account_id: str = typer.Option("", "--account", "-a", help="可选:带上该账号的上下文。"),
    chat_id: str = typer.Option("", "--chat-id", help="可选:带上该会话的历史(配合 --account)。"),
    item_id: str = typer.Option("", "--item-id", help="可选:带上该商品的本地事实。"),
    provider_type: str = typer.Option("", "--type", help="只测某一类 Provider;留空用启用中的那个。"),
) -> None:
    """用一条假消息试跑 AI 回复(对照 butler 的 ai-reply-test)。

    这会**真的调用**你配置的 Provider(消耗额度),但不会发给任何买家、不写消息镜像、不写
    回复记录——它只回答两个问题:"密钥与模型能用吗"和"模型这句话过不过我们的输出安全闸"。
    """

    async def _run() -> None:
        settings = get_settings()
        if settings.reply_mode not in {"RULE_THEN_AI", "RULE_DEFAULT_THEN_AI", "AI_ONLY"}:
            console.print(
                f"[yellow]当前回复模式是 {settings.reply_mode},自动回复不会调用 AI;"
                "下面测的是 Provider 本身能不能用。[/yellow]"
            )
        context = AIConversationContext(messages=(), item_facts=None)
        if account_id and (chat_id or item_id):
            context = await ContextService().build(
                account_id=account_id,
                chat_id=chat_id,
                current_message_id=None,
                item_id=item_id or None,
            )
        request = AIRequest(
            account_id=account_id or "__ai_test__",
            buyer_message=message,
            chat_context=context.messages,
            item_facts=context.item_facts,
        )
        try:
            response = await VaultBackedProvider(
                provider_type=provider_type or None
            ).generate(request)
        except UnconfiguredError as exc:
            console.print(f"[red]AI Provider 未配置或密钥不可用:[/red] {exc}")
            console.print(
                "[dim]先 `ai add ...` 写配置与密钥,再用 `ai enable` 启用;"
                "`ai list` 可以看到有没有启用中的 Provider。[/dim]"
            )
            raise typer.Exit(code=1) from exc
        except Exception as exc:
            # 异常正文可能带请求细节,这里只给类型与排查方向。
            console.print(f"[red]调用失败:[/red] {type(exc).__name__}")
            console.print("[dim]检查密钥是否有效、模型名与 Base URL 是否正确。[/dim]")
            raise typer.Exit(code=1) from exc
        decision = SafetyPolicy().evaluate(response.text)
        console.print(f"[cyan]模型:[/cyan] {response.provider or '-'} / {response.model or '-'}")
        console.print(f"[cyan]草稿:[/cyan] {response.text.strip() or '(空)'}")
        if decision.allowed:
            console.print("[green]OK[/green] 这条草稿会通过输出安全闸(仅表示可以发,不代表已发)。")
        else:
            console.print(f"[red]会被输出安全闸拦截:[/red] {decision.reason}")
            raise typer.Exit(code=1)

    asyncio.run(_run())
