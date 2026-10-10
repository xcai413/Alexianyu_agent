"""CLI 表面契约:每个命令与子命令都必须能正常响应 `--help`。

命令定义写错(选项名冲突、默认值构造非法、缺少类型注解等)通常只在用户真的敲到那条命令时才炸。
这里遍历整棵 Typer 命令树逐条跑 `--help`,把"某条命令根本起不来"提前变成测试失败。
"""

from __future__ import annotations

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from xianyu_agent.cli.main import app

runner = CliRunner()


def _walk(group: object, prefix: list[str]) -> list[list[str]]:
    paths: list[list[str]] = []
    commands = getattr(group, "commands", None)
    if not commands:
        return paths
    for name, command in sorted(commands.items()):
        path = [*prefix, name]
        paths.append(path)
        # Typer 的子组是 TyperGroup,既不是 click.Group 也不是 click.Command;
        # 在这个 click 版本上 isinstance 判断不可靠,所以只按"有没有 commands"判断。
        if getattr(command, "commands", None):
            paths.extend(_walk(command, path))
    return paths


def _command_paths() -> list[list[str]]:
    return _walk(get_command(app), [])


def test_command_tree_is_not_empty() -> None:
    paths = _command_paths()
    assert len(paths) > 40, f"命令树只解析到 {len(paths)} 条命令,解析逻辑可能失效"


@pytest.mark.parametrize("path", _command_paths(), ids=" ".join)
def test_every_command_answers_help(path: list[str]) -> None:
    result = runner.invoke(app, [*path, "--help"])

    assert result.exit_code == 0, f"{' '.join(path)} --help 失败:\n{result.output}"
    assert "Usage" in result.output


def _params(path: list[str]) -> set[str]:
    node = get_command(app)
    for name in path:
        node = node.commands[name]
    return {param.name for param in node.params}


def test_no_two_commands_share_one_function() -> None:
    """一个函数挂两个命令名(堆叠装饰器)必然让其中一个的命令行契约名不副实。

    真实案例:`delivery job resolve` 曾经和 `plan` 挂在同一个函数上,于是"人工收尾"实际执行的是
    发货 Dry Run,而真正的收尾实现成了死代码——`--help` 契约测试发现不了这种错(两条命令都能
    正常输出帮助,只是参数是另一条的)。这里按"函数对象是否重复"直接拦截。
    """
    seen: dict[object, str] = {}
    duplicated: list[str] = []
    for path in _command_paths():
        node = get_command(app)
        for name in path:
            node = node.commands[name]
        callback = getattr(node, "callback", None)
        if callback is None:
            continue
        label = " ".join(path)
        if callback in seen:
            duplicated.append(f"{label} 与 {seen[callback]} 是同一个函数")
            continue
        seen[callback] = label

    assert not duplicated, "\n".join(duplicated)


def test_delivery_job_resolve_keeps_its_human_verdict_surface() -> None:
    params = _params(["delivery", "job", "resolve"])

    assert "job_id" in params
    assert {"delivered", "note", "yes"} <= params


def test_delivery_job_plan_keeps_its_dry_run_surface() -> None:
    params = _params(["delivery", "job", "plan"])

    assert {"account_id", "order_id"} <= params
    assert "delivered" not in params
