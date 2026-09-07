"""供 Windows 任务计划以 pythonw 启动的无交互入口。"""

from __future__ import annotations

import asyncio
import logging

from xianyu_agent.runtime.daemon import run_runtime_daemon
from xianyu_agent.services.windows_service import is_service_paused

logger = logging.getLogger(__name__)


def main() -> int:
    if is_service_paused():
        return 0
    try:
        asyncio.run(run_runtime_daemon())
    except Exception:
        logger.exception("scheduled daemon runner failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
