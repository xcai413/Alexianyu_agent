"""隔离容器的 Web/MCP 只读与会话撤销探针;凭据仅从测试环境读取。"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mcp import ClientSession
from mcp.client.sse import sse_client

BASE_URL = "http://127.0.0.1:8090"


def request(path, *, payload=None, cookie=None, csrf=None):
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = Request(BASE_URL + path, data=data, headers=headers)
    try:
        with urlopen(req, timeout=10) as response:
            return response.status, response.read(), response.headers
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers


async def probe_mcp(endpoint):
    async with (
        sse_client(BASE_URL + endpoint) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert len(tools.tools) >= 50
        result = await session.call_tool("summary_api_v1_dashboard_summary_get", {})
        assert result.isError, "MCP 管理工具缺少会话时必须拒绝"
        return len(tools.tools)


def main():
    password = os.environ.get("XIANYU_SMOKE_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("仅用于隔离验收:必须设置 XIANYU_SMOKE_ADMIN_PASSWORD")
    status, html, _ = request("/")
    assert status == 200
    assert b'id="root"' in html
    status, _, _ = request("/api/v1/dashboard/summary")
    assert status == 401
    status, body, headers = request("/api/v1/web/auth/login", payload={"password": password})
    assert status == 200
    data = json.loads(body)["data"]
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    csrf = data["csrf_token"]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=strict" in headers["Set-Cookie"]
    status, _, _ = request("/api/v1/dashboard/summary", cookie=cookie)
    assert status == 200
    status, _, _ = request("/api/v1/web/auth/logout", payload={}, cookie=cookie)
    assert status == 403
    status, _, _ = request("/api/v1/web/auth/logout", payload={}, cookie=cookie, csrf=csrf)
    assert status == 200
    status, _, _ = request("/api/v1/dashboard/summary", cookie=cookie)
    assert status == 401
    status, body, _ = request("/api/v1/web/auth/session", cookie=cookie)
    assert status == 200
    assert json.loads(body)["data"]["authenticated"] is False
    tool_counts = {endpoint: asyncio.run(probe_mcp(endpoint)) for endpoint in ("/mcp", "/sse")}
    print(
        json.dumps({"web": "passed", "copied_logout_session": "revoked", "mcp_tools": tool_counts})
    )


if __name__ == "__main__":
    main()
