"""Skill 文件是 Agent 的行动契约:里面写的命令与参数必须真实存在。

这些文件曾经出现过 `pool start-all --seconds 3600` 这种不存在的参数——Agent 照着做只会拿到
usage error。所以这里把"skill 里出现的命令/参数"当成契约来校验,让文档过期在测试里暴露。

解析规则(刻意保守,避免误报):

- 一行里只取第一个 `xianyu-agent` 之后到行尾的内容;
- 沿 Typer 命令树逐词下钻,遇到选项、位置参数或不可识别的词就停止下钻;
- 停止时如果当前节点仍是命令组、且下一个词既不是选项也不是位置参数,才判定"未知子命令";
- 选项按解析到的那条叶子命令校验;`a/b` 这种写法按多个候选分别校验。
"""

from __future__ import annotations

import re
from pathlib import Path

import click
from typer.main import get_command

from xianyu_agent.cli.main import app

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "xianyu_agent" / "skills"
REFERENCE = re.compile(r"xianyu-agent\s+(\S.*)$")
FLAG = re.compile(r"^--[a-z0-9-]+$")
# 行内代码/括号/箭头之后就属于说明文字,不再属于命令本身。
REFERENCE_END = re.compile(r"[`()\u3002\u3001\uff09\u2192;|]")


def _root() -> click.Group:
    command = get_command(app)
    # Typer 生成的是 TyperGroup,它不是 click.Group 的子类,但同样有 commands 映射。
    assert hasattr(command, "commands")
    return command  # type: ignore[return-value]


def _flags_of(command: object) -> set[str]:
    names = {"--help"}
    for parameter in getattr(command, "params", []):
        names.update(opt for opt in getattr(parameter, "opts", []) if opt.startswith("--"))
    return names


def _is_placeholder(word: str) -> bool:
    return word.startswith(("<", "[")) or word.endswith("...")


def _describe(word: str) -> list[str]:
    """`enable/disable` 这类写法代表多个候选。"""
    return [part for part in word.split("/") if part]


def _iter_references() -> list[tuple[Path, str, list[str]]]:
    references: list[tuple[Path, str, list[str]]] = []
    for skill_path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        for line in skill_path.read_text(encoding="utf-8").splitlines():
            match = REFERENCE.search(line)
            if match is None:
                continue
            tail = REFERENCE_END.split(match.group(1))[0]
            words = [word.strip(",") for word in tail.split()]
            references.append((skill_path, line.strip(), [word for word in words if word]))
    return references


def test_skill_files_exist() -> None:
    assert sorted(SKILLS_DIR.glob("*/SKILL.md")), "没有找到任何 Skill 文件"


def test_skill_references_are_parseable() -> None:
    assert _iter_references(), "Skill 里没有解析到任何 xianyu-agent 命令"


def test_every_referenced_command_and_flag_exists() -> None:
    problems: list[str] = []
    for skill_path, line, words in _iter_references():
        current: object = _root()
        consumed = 0
        for word in words:
            if word.startswith("-") or _is_placeholder(word):
                break
            candidates = _describe(word)
            commands = getattr(current, "commands", None)
            if commands is None or any(name not in commands for name in candidates):
                if commands is not None:
                    problems.append(f"{skill_path.parent.name}: 未知子命令 {word!r}({line})")
                break
            current = commands[candidates[0]]
            consumed += 1
        flags = {word for word in words if FLAG.match(word)}
        unknown = flags - _flags_of(current)
        if unknown:
            problems.append(
                f"{skill_path.parent.name}: {' '.join(words[:consumed])} 不支持 "
                f"{'、'.join(sorted(unknown))}({line})"
            )
    assert not problems, "Skill 引用了不存在的命令或参数:\n" + "\n".join(problems)
