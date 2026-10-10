"""前端与后端路由的契约:界面里 `fetch` 的每个路径都必须是真实存在的端点。

Web 控制台的请求路径都是写死的字符串/模板串,端点一旦改名或删除,界面只会在人点下去时静默失败。
这里把两边的路径都归一化成"分段通配"再比对,让这类漂移在 CI 里暴露。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xianyu_agent.api.app import app as api_app

WEB_APP = Path(__file__).resolve().parents[2] / "frontend" / "src" / "WebApp.tsx"
# 单引号、双引号、模板串三种写法都可能出现在 fetch 里。
PATH_LITERAL = re.compile(r"['\"`](/api/v1/[^'\"`]*)[\"'`]")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{[^}]*\}")


def _segments(path: str) -> list[str]:
    """把查询串去掉、把参数占位符统一成 `*`,返回路径分段。"""
    without_query = path.split("?", 1)[0].strip()
    plain = PLACEHOLDER.sub("*", without_query)
    return [segment for segment in plain.split("/") if segment]


def _registered_routes() -> set[tuple[str, ...]]:
    """从 OpenAPI schema 取全部路径。

    `app.routes` 里被 include 的子路由是 `_IncludedRouter` 包装,拿不到 path,所以不能直接遍历
    `app.routes`;schema 是唯一权威的完整路径清单。
    """
    schema = api_app.openapi()
    return {tuple(_segments(path)) for path in schema.get("paths", {})}


def _frontend_paths() -> list[str]:
    return sorted(set(PATH_LITERAL.findall(WEB_APP.read_text(encoding="utf-8"))))


def test_frontend_references_are_discovered() -> None:
    paths = _frontend_paths()
    assert len(paths) > 20, f"只在前端找到 {len(paths)} 个 API 路径,解析逻辑可能失效"


def test_frontend_paths_match_registered_routes() -> None:
    registered = _registered_routes()
    problems = [
        path for path in _frontend_paths() if tuple(_segments(path)) not in registered
    ]
    assert not problems, "前端引用了不存在的 API 路径:\n" + "\n".join(problems)


@pytest.mark.parametrize("path", _frontend_paths())
def test_frontend_path_is_reachable_in_the_api(path: str) -> None:
    """再逐条断言一次,失败时能直接指出是哪条路径。"""
    assert tuple(_segments(path)) in _registered_routes()
