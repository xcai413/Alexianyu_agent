#!/usr/bin/env python3
"""分别检查 daemon 心跳,或 Web 服务、认证配置与撤销表可读性。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.request import urlopen

from sqlalchemy import select

from xianyu_agent.db import database as db
from xianyu_agent.infrastructure.database.models import RevokedWebSession


async def _check_web_database() -> None:
    try:
        async with db.get_async_session() as session:
            await session.execute(select(RevokedWebSession.session_hash).limit(1))
    finally:
        await db.async_engine.dispose()


def _check() -> str | None:
    if os.environ.get("XIANYU_CONTAINER_SERVICE") == "web":
        port = int(os.environ.get("XIANYU_MCP_PORT", "8090"))
        with urlopen(f"http://127.0.0.1:{port}/api/v1/web/auth/session", timeout=5) as response:
            payload = json.loads(response.read())
        if response.status != 200 or payload.get("data", {}).get("authenticated") is not False:
            return "Web 会话探针响应异常"
        # 无 Cookie 的 /session 不查询数据库,单独验证撤销表,避免数据库失联仍报告健康。
        asyncio.run(_check_web_database())
        return None
    port = int(os.environ.get("XIANYU_HTTP_PORT", "8089"))
    with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
        payload = json.loads(response.read())
    if response.status == 200 and payload.get("status") == "healthy":
        return None
    return f"daemon 不健康:{payload}"


def main() -> int:
    try:
        problem = _check()
    except Exception as exc:
        # 数据库异常可能含连接信息;健康输出只保留异常类型。
        print(f"健康检查失败:{type(exc).__name__}", file=sys.stderr)
        return 1
    if problem is None:
        print("healthy")
        return 0
    print(problem, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
