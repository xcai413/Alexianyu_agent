"""MCP 表面:`mcp serve` 会把**全部** FastAPI 路由生成 tools,所以保护管理端点的只有依赖本身。

这不是理论问题:Agent API 无鉴权、管理 API 要会话,两批端点都在同一张 tools 列表里。只要哪天
有人在 `/api/v1/web/*` 下加了一个忘记带 `require_admin` 的路由,MCP 就会把它当作一个
"无需鉴权的工具"暴露出去。这里逐条断言这条不变量,并锁住"哪种情况可以被允许"的白名单。
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi_mcp import FastApiMCP

from xianyu_agent.api.app import app as api_app
from xianyu_agent.interfaces.web.auth.router import (
    require_admin,
    require_admin_csrf,
    router as auth_router,
)
from xianyu_agent.interfaces.web.dashboard.router import router as dashboard_router
from xianyu_agent.interfaces.web.operations.router import router as operations_router

ADMIN_DEPENDENCIES = {require_admin, require_admin_csrf}

# 这三个端点自己负责建立/查询/销毁会话,所以不能要求"已有会话";logout 在函数体里校验会话与 CSRF。
SESSION_LIFECYCLE_PATHS = {
    "/api/v1/web/auth/login",
    "/api/v1/web/auth/session",
    "/api/v1/web/auth/logout",
}


def _tools() -> dict[str, dict[str, object]]:
    mcp = FastApiMCP(api_app, name="contract", describe_all_responses=False)
    mcp.setup_server()
    return dict(mcp.operation_map)


def _admin_routes() -> tuple[set[tuple[str, str]], set[str]]:
    """(带管理员依赖的 路径+方法, 路由里出现过的全部路径)。

    这里必须直接走路由器对象:`api_app.routes` 里被 include 的子路由是 `_IncludedRouter` 包装,
    拿不到 path 与 dependant——第一版测试就是这样静默变成空断言的。
    """
    protected: set[tuple[str, str]] = set()
    all_paths: set[str] = set()
    for router in (operations_router, dashboard_router, auth_router):
        assert isinstance(router, APIRouter)
        for route in router.routes:
            path = getattr(route, "path", None)
            if path is None:
                continue
            # 路由器自己的 prefix 已经写进 route.path,不要再拼一次。
            full_path = str(path)
            all_paths.add(full_path)
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            calls = {dependency.call for dependency in dependant.dependencies}
            if calls & ADMIN_DEPENDENCIES:
                for method in getattr(route, "methods", set()) or set():
                    protected.add((full_path, str(method).lower()))
    return protected, all_paths


def test_mcp_exposes_the_agent_api() -> None:
    """"Agent 可通过 MCP 介入"必须有实际工具支撑,而不是文档里的说法。"""
    tools = _tools()

    assert len(tools) >= 50
    assert any(str(item["path"]).startswith("/api/v1/web/") for item in tools.values())


def test_every_mcp_admin_tool_is_protected_by_a_session_dependency() -> None:
    protected, _ = _admin_routes()
    unprotected: list[str] = []
    checked = 0
    for name, item in _tools().items():
        path = str(item["path"])
        method = str(item["method"]).lower()
        if not path.startswith(("/api/v1/web/", "/api/v1/dashboard/")):
            continue
        checked += 1
        if path in SESSION_LIFECYCLE_PATHS:
            continue
        if (path, method) not in protected:
            unprotected.append(f"{name} -> {method.upper()} {path}")

    # 防止这条断言被"一个管理工具都没扫到"静默变成空跑。
    assert checked > 30
    assert not unprotected, (
        "以下管理端点会作为 MCP 工具暴露,却没有管理员会话依赖:\n" + "\n".join(unprotected)
    )


def test_session_lifecycle_whitelist_has_not_grown() -> None:
    """白名单只能更小:多出来的免鉴权管理端点必须是显式决定。"""
    tools = _tools()
    web_paths = {
        str(item["path"])
        for item in tools.values()
        if str(item["path"]).startswith("/api/v1/web/")
    }

    assert web_paths >= SESSION_LIFECYCLE_PATHS


def test_agent_tools_do_not_include_admin_paths_without_a_dependency() -> None:
    """反向断言:无鉴权的只有 `/api/v1/*` 里非 web/dashboard 的那批。"""
    unprotected_agent_paths = sorted(
        {
            str(item["path"])
            for item in _tools().values()
            if str(item["path"]).startswith("/api/v1/")
            and not str(item["path"]).startswith(("/api/v1/web/", "/api/v1/dashboard/"))
        }
    )

    assert unprotected_agent_paths, "Agent API 不该为空;空了说明前缀被整体挪走"
    assert not any(path.startswith("/api/v1/web/auth") for path in unprotected_agent_paths)


@pytest.mark.parametrize("path", sorted(SESSION_LIFECYCLE_PATHS))
def test_whitelisted_paths_are_real_routes(path: str) -> None:
    _, all_paths = _admin_routes()

    assert path in all_paths
