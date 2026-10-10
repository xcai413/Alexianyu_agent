"""拒绝路径也只能"好好报错":命令不许把内部异常抛到用户面前。

全新库里没有账号、没有 Cookie、没有订单,任何命令都不可能真的外发。所以每一条都该走"明确拒绝"
这条路:要么干净退出并说明原因,要么抛 `typer.Exit`。抛别的异常(状态机非法转移、KeyError、
未处理空值……)在真实终端上就是一段 Rich 堆栈,用户看不出这是"你的订单不在镜像里"还是"程序坏了"。

这组用例抓的是"逻辑顺序写反"这一类缺陷——例如执行器先判断拒绝、再切状态,导致 pending → blocked
这种非法转移(2026-09-21 真实存在过)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import typer
from alembic import command
from alembic.config import Config
from click.exceptions import Exit as ClickExit
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts

runner = CliRunner()
ACCOUNT = "acc-reject"

# 全部是"明知会失败"的调用:不存在的订单/任务/池/渠道/会话。
REJECT_PATHS: tuple[list[str], ...] = (
    # 发货:校验拒绝、状态机拒绝、找不到记录
    ["delivery", "job", "run", "-a", ACCOUNT, "--order-id", "O-404"],
    ["delivery", "job", "show", "404"],
    ["delivery", "job", "retry", "404", "--yes"],
    ["delivery", "job", "cancel", "404", "--yes"],
    ["delivery", "job", "resolve", "404", "--delivered", "--note", "x", "--yes"],
    ["delivery", "job", "recover"],
    ["delivery", "policy", "show", "404"],
    ["delivery", "policy", "delete", "404", "--yes"],
    ["delivery", "template", "show", "404"],
    ["delivery", "api", "show", "404"],
    ["delivery", "api", "test", "404"],
    ["delivery", "guard", "delete", "404", "--yes"],
    ["delivery", "blacklist", "remove", "B-404"],
    # 库存
    ["inventory", "pool", "show", "404"],
    ["inventory", "pool", "disable", "404"],
    ["inventory", "unit", "list", "404"],
    ["inventory", "reservation", "show", "404"],
    ["inventory", "reservation", "consume", "404"],
    ["inventory", "reservation", "release", "404"],
    # 订单 / 商品
    ["order", "show", "404"],
    ["item", "show", "404"],
    ["item", "automation", "list", "-a", ACCOUNT],
    ["item", "bindings", "-a", ACCOUNT, "--item-id", "I-404"],
    # 客服
    ["message", "takeover", "-a", ACCOUNT, "--chat-id", "c-404"],
    ["message", "note", "show", "-a", ACCOUNT, "--buyer-id", "B-404"],
    ["message", "quick-reply", "delete", "404"],
    ["rule", "disable", "404"],
    ["rule", "delete", "404", "--yes"],
    ["rule", "logs", "-a", "missing-account"],
    # 账号 / 通知 / AI
    ["account", "show", "--id", "missing-account"],
    ["auth", "status", "-a", "missing-account"],
    ["notify", "channel", "show", "404"],
    ["notify", "channel", "test", "404"],
    ["notify", "channel", "bind", "404", "-a", "missing-account"],
    ["ai", "enable", "--name", "missing-provider"],
)


def _alembic_config(db_path: Path) -> Config:
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


@pytest.fixture
def reject_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "reject.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    reset_settings_cache()
    db_mod.reset_engine()
    command.upgrade(_alembic_config(db_path), "head")
    asyncio.run(domain_accounts.create_account(ACCOUNT, enabled=False))
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


@pytest.mark.parametrize("args", REJECT_PATHS, ids=" ".join)
def test_reject_path_fails_cleanly(reject_db: Path, args: list[str]) -> None:
    result = runner.invoke(app, args)

    _assert_clean_failure(result, args)


def _assert_clean_failure(result: object, args: list[str]) -> None:
    """`typer.Exit` / `SystemExit` 是"我们主动告诉用户不行";其它异常都是没接住的内部错误。"""
    exception = getattr(result, "exception", None)
    output = getattr(result, "output", "")
    allowed = (typer.Exit, ClickExit, SystemExit)
    assert exception is None or isinstance(exception, allowed), (
        f"{' '.join(args)} 抛出了未处理异常 {type(exception).__name__}: {exception}\n{output}"
    )
    assert "Traceback" not in output, f"{' '.join(args)} 打印了堆栈:\n{output}"


class _FakeResult:
    """只带断言需要的两个字段,用来验证这组断言不是空跑。"""

    def __init__(self, exception: BaseException, output: str) -> None:
        self.exception = exception
        self.output = output


def test_guard_itself_rejects_an_unhandled_exception() -> None:
    """注入一个内部异常,断言必须失败——否则上面的用例只是"碰巧都通过了"。"""
    injected = _FakeResult(RuntimeError("boom"), "Traceback (most recent call last): ...")

    with pytest.raises(AssertionError):
        _assert_clean_failure(injected, ["fake", "command"])


def test_guard_itself_accepts_a_clean_refusal() -> None:
    clean = _FakeResult(typer.Exit(1), "账号不存在")

    _assert_clean_failure(clean, ["fake", "command"])
