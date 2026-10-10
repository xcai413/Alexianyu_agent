"""实际CLI装配不能让打包后的SPA吞掉MCP传输路由。"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Mount

from xianyu_agent.api.app import mount_web_console
from xianyu_agent.cli.commands import mcp as mcp_cli


@pytest.mark.parametrize("endpoint", ["/mcp", "/sse"])
async def test_mcp_transport_is_reachable_with_built_spa(tmp_path, monkeypatch, endpoint):
    (tmp_path / "index.html").write_text('<div id="root">console</div>')
    application = FastAPI()

    @application.get("/api/v1/example")
    def example():
        return {"success": True}

    mount_web_console(application, static_dir=tmp_path)
    monkeypatch.setattr(mcp_cli, "fastapi_app", application)
    monkeypatch.setattr(mcp_cli.uvicorn, "run", lambda *_, **__: None)
    mcp_cli.serve(port=8090, host="127.0.0.1")
    assert isinstance(application.routes[-1], Mount)
    assert application.routes[-1].name == "web-console"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        # 无效SSE会话应由MCP接收端拒绝,不能落入SPA静态文件服务。
        response = await client.post(endpoint + "/messages/", json={})
        assert response.status_code == 400
        assert "text/html" not in response.headers.get("content-type", "")
        assert (await client.get("/")).text == '<div id="root">console</div>'
        assert (await client.get("/api/v1/example")).json() == {"success": True}
