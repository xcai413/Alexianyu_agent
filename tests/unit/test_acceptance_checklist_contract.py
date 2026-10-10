"""验收清单里的命令必须真的是命令。

`docs/待真实验证清单.md` 是执行人照着敲的那份文件:命令改名、子命令挪走、参数换掉,清单不会自己
更新,而执行人会在真机上手忙脚乱地怀疑是自己环境的问题。这里把清单里出现的
`xianyu-agent <命令路径>` 与真实命令树逐条比对。

只校验**命令路径**(命令与子命令名),不校验选项与参数值:清单里带 `<账号>` 这类占位符,参数形状
由各命令自己的测试负责。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.main import get_command

from xianyu_agent.cli.main import app

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
# 两份都是"执行人照着敲"的文件:一份按风险排序的验收清单,一份更粗粒度的功能清单。
CHECKLISTS: tuple[Path, ...] = (
    DOCS_DIR / "待真实验证清单.md",
    DOCS_DIR / "真实功能测试清单.md",
)

# `xianyu-agent foo bar` —— 取命令名与每级子命令名,遇到选项、占位符或非标识符就停。
COMMAND = re.compile(r"xianyu-agent\s+([A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*)")


def _command_paths() -> set[tuple[str, ...]]:
    """命令树里所有合法的命令前缀,含中间层子组。"""
    paths: set[tuple[str, ...]] = set()

    def walk(node: object, prefix: tuple[str, ...]) -> None:
        for name, command in sorted(getattr(node, "commands", {}).items()):
            path = (*prefix, name)
            paths.add(path)
            if getattr(command, "commands", None):
                walk(command, path)

    walk(get_command(app), ())
    return paths


def _checklist_invocations() -> list[tuple[str, int, tuple[str, ...]]]:
    found: list[tuple[str, int, tuple[str, ...]]] = []
    for checklist in CHECKLISTS:
        text = checklist.read_text(encoding="utf-8")
        found.extend(
            (checklist.name, lineno, tuple(match.group(1).split()))
            for lineno, line in enumerate(text.splitlines(), start=1)
            for match in COMMAND.finditer(line)
        )
    return found


def _longest_known(path: tuple[str, ...], known: set[tuple[str, ...]]) -> tuple[str, ...]:
    """命令后面可能跟的是参数(例如 `xianyu-agent account show test-account`),取最长已知前缀。"""
    for length in range(len(path), 0, -1):
        candidate = path[:length]
        if candidate in known:
            return candidate
    return ()


def test_checklist_exists_and_mentions_enough_commands() -> None:
    """防止清单被改名后这条测试静默变成空跑。"""
    for checklist in CHECKLISTS:
        assert checklist.is_file(), f"找不到 {checklist}"
    # 两份清单目前一共解析到 31 处命令调用(它们写的是"跑什么、期望什么",不是命令大全)。
    assert len(_checklist_invocations()) > 25


@pytest.mark.parametrize(
    ("source", "lineno", "path"),
    _checklist_invocations(),
    ids=str,
)
def test_checklist_command_path_exists(
    source: str, lineno: int, path: tuple[str, ...]
) -> None:
    known = _command_paths()

    resolved = _longest_known(path, known)

    assert resolved, f"{source}:{lineno} 提到的命令 {' '.join(path)} 在命令树里不存在"
