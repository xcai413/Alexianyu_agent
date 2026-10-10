"""接口鉴权边界:管理端点必须全部要管理员会话,Agent API 的无鉴权范围必须是有意为之。

`docs/api-reference.md` 明确写了"Agent HTTP API 是局域网内部服务、未加鉴权",而 Web 管理端点必须
带管理员会话与 CSRF。这条边界此前只靠文档和人工记忆维持:新增一个忘记加依赖的管理端点,或者不小心
把管理路由挪到无鉴权前缀下,都不会有任何测试报警。这里把边界固化成断言。
"""

from __future__ import annotations

import pytest

from xianyu_agent.api.app import app as api_app
from xianyu_agent.interfaces.web.auth.router import require_admin, require_admin_csrf
from xianyu_agent.interfaces.web.dashboard.router import router as dashboard_router
from xianyu_agent.interfaces.web.operations.router import router as operations_router

ADMIN_DEPENDENCIES = {require_admin, require_admin_csrf}


def _route_dependency_calls(route: object) -> set[object]:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return set()
    return {dependency.call for dependency in dependant.dependencies}


@pytest.mark.parametrize(
    "router",
    [operations_router, dashboard_router],
    ids=["operations", "dashboard"],
)
def test_admin_routers_require_a_session_on_every_route(router: object) -> None:
    unprotected: list[str] = []
    for route in router.routes:
        path = getattr(route, "path", "?")
        if not _route_dependency_calls(route) & ADMIN_DEPENDENCIES:
            unprotected.append(path)

    assert not unprotected, "以下管理端点缺少管理员会话依赖:\n" + "\n".join(unprotected)


def test_admin_routers_actually_have_routes() -> None:
    """防止路由被挪走后这个测试变成空跑。"""
    assert len(operations_router.routes) > 40
    assert len(dashboard_router.routes) >= 2


def test_agent_api_is_unauthenticated_and_that_is_the_documented_boundary() -> None:
    """Agent API 的无鉴权范围必须与文档一致:出现在 OpenAPI 里的 `/api/v1/*` 不带管理员依赖。

    这不是"漏洞",而是 `docs/api-reference.md` 明确写下的边界(局域网内部服务)。测试在这里的作用是
    让边界变化可见:谁把管理端点挪进这个前缀、或者给 Agent API 加上鉴权,都必须同步改文档与测试。
    """
    schema = api_app.openapi()
    agent_paths = sorted(
        path
        for path in schema["paths"]
        if path.startswith("/api/v1/")
        and not path.startswith("/api/v1/web/")
        and not path.startswith("/api/v1/dashboard/")
    )

    assert agent_paths, "没有解析到 Agent API 路径,解析逻辑可能失效"
    # 管理前缀必须独立存在;把它混进 Agent API 会同时放大暴露面。
    assert not any(path.startswith("/api/v1/web/") for path in agent_paths)
    # 记录当前规模,防止无意间大幅扩张无鉴权写接口而无人察觉。
    assert len(agent_paths) <= 40, f"无鉴权 Agent API 路径数量异常增长: {len(agent_paths)}"
