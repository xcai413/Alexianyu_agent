"""Windows 任务计划每分钟调用的一次性 daemon watchdog。"""

from __future__ import annotations

import asyncio

from xianyu_agent.application.health.runtime_health import observe_daemon
from xianyu_agent.application.health.soak import sample_active_soaks
from xianyu_agent.domain import daemon as daemon_domain
from xianyu_agent.runtime.platform.windows_service import (
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
        asyncio.run(_run_once())
    except Exception:
        return 1
    return 0


async def _run_once() -> None:
    await check_and_recover()
    await sample_active_soaks()


if __name__ == "__main__":
    raise SystemExit(main())
