"""Windows 任务计划每分钟调用的一次性 daemon watchdog。"""

from __future__ import annotations

import asyncio

from xianyu_agent.domain import daemon as daemon_domain
from xianyu_agent.services.daemon_health import observe_daemon
from xianyu_agent.services.windows_service import (
    WindowsServiceError,
    end_task,
    is_service_paused,
    start_task,
)


async def check_and_recover() -> bool:
    """不健康时重启任务;返回是否触发了恢复。"""
    if is_service_paused():
        return False
    row = await daemon_domain.latest_instance()
    health = observe_daemon(row)
    if health.healthy:
        return False
    if row is not None and health.active and health.process_alive:
        if health.observed != "stale":
            return False
        try:
            end_task()
        except WindowsServiceError:
            return False
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not observe_daemon(row).process_alive:
                break
        else:
            return False
    start_task()
    return True


def main() -> int:
    try:
        asyncio.run(check_and_recover())
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
