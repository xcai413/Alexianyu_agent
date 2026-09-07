"""持有账号池生命周期的常驻 RuntimeDaemon。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from xianyu_agent import __version__
from xianyu_agent.config import get_settings
from xianyu_agent.domain import (
    accounts as domain_accounts,
    daemon as daemon_domain,
    worker_commands,
    worker_risk,
)
from xianyu_agent.runtime.daemon_lock import DaemonLock
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.logging_setup import configure_daemon_logging

logger = logging.getLogger(__name__)
PoolFactory = Callable[[], Awaitable[AccountPool]]


class RuntimeDaemon:
    """单进程管理全部启用账号的长期运行协调器。"""

    def __init__(
        self,
        *,
        heartbeat_interval_s: float = 10.0,
        reconcile_interval_s: float = 5.0,
        pool_factory: PoolFactory | None = None,
        lock: DaemonLock | None = None,
        install_signal_handlers: bool = True,
        configure_logging: bool = True,
    ) -> None:
        settings = get_settings()
        self.instance_id = uuid.uuid4().hex
        self.heartbeat_interval_s = heartbeat_interval_s
        self.reconcile_interval_s = reconcile_interval_s
        self._pool_factory = pool_factory or AccountPool.from_desired_accounts
        self._lock = lock or DaemonLock(settings.daemon_lock_path)
        self._install_signals = install_signal_handlers
        self._configure_logging = configure_logging
        self._stop_event = asyncio.Event()
        self._pool: AccountPool | None = None
        self._instance_created = False
        self._signal_registrations: list[tuple[str, signal.Signals, Any]] = []

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> bool:
        """运行到停止;返回 True 表示收到重启请求。"""
        settings = get_settings()
        self._lock.acquire(instance_id=self.instance_id)
        restart_requested = False
        fatal_error: str | None = None
        try:
            if self._configure_logging:
                configure_daemon_logging(settings.daemon_log_path, level=settings.log_level)
            if self._install_signals:
                self._register_signal_handlers()
            await self._start()
            restart_requested = await self._serve()
        except asyncio.CancelledError:
            self.request_stop()
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
            logger.exception("daemon fatal error instance=%s", self.instance_id)
            raise
        finally:
            await self._shutdown(fatal_error=fatal_error, restart_requested=restart_requested)
        return restart_requested

    async def _start(self) -> None:
        orphaned = await daemon_domain.mark_orphaned_active(
            reason="new daemon acquired singleton lock after previous abnormal exit"
        )
        if orphaned:
            logger.warning("marked %d orphaned daemon instance(s) as error", orphaned)
        await daemon_domain.create_instance(
            instance_id=self.instance_id,
            pid=os.getpid(),
            version=__version__,
        )
        self._instance_created = True
        stale = await worker_commands.fail_stale_running(
            reason="previous daemon exited before command completion"
        )
        if stale:
            logger.warning("marked %d stale worker command(s) as failed", stale)
        self._pool = await self._pool_factory()
        started = self._pool.start_all()
        await daemon_domain.mark_running(self.instance_id)
        logger.info(
            "daemon started instance=%s pid=%d workers=%s",
            self.instance_id,
            os.getpid(),
            ",".join(started) or "(none)",
        )

    async def _serve(self) -> bool:
        if self._pool is None:  # pragma: no cover - guarded by _start
            return False
        next_reconcile_at = time.monotonic() + self.reconcile_interval_s
        while not self._stop_event.is_set():
            control = await daemon_domain.touch_heartbeat(self.instance_id)
            if control.shutdown_requested:
                self.request_stop()
                return control.restart_requested
            await self._process_worker_commands()
            now = time.monotonic()
            if now >= next_reconcile_at:
                await self._reconcile()
                next_reconcile_at = now + self.reconcile_interval_s
            timeout = min(self.heartbeat_interval_s, self.reconcile_interval_s)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        return False

    async def _reconcile(self) -> None:
        if self._pool is None:  # pragma: no cover - guarded by _start
            return
        changes = await self._pool.reconcile_enabled_accounts()
        if changes["started"] or changes["stopped"]:
            logger.info(
                "worker reconciliation started=%s stopped=%s",
                changes["started"],
                changes["stopped"],
            )

    async def _process_worker_commands(self) -> None:
        if self._pool is None:  # pragma: no cover - guarded by _start
            return
        for command in await worker_commands.pending():
            if not await worker_commands.claim(
                command.command_id, daemon_instance_id=self.instance_id
            ):
                continue
            account_id = await worker_commands.account_key(command)
            if account_id is None:
                await worker_commands.complete(
                    command.command_id,
                    success=False,
                    error="account no longer exists",
                )
                continue
            try:
                result = await self._execute_worker_command(account_id, command.action)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "worker command failed command=%s account=%s action=%s",
                    command.command_id,
                    account_id,
                    command.action,
                )
                await worker_commands.complete(
                    command.command_id,
                    success=False,
                    error=error,
                )
                continue
            await worker_commands.complete(
                command.command_id,
                success=True,
                result=result,
            )
            logger.info(
                "worker command succeeded command=%s account=%s action=%s result=%s",
                command.command_id,
                account_id,
                command.action,
                result,
            )

    async def _execute_worker_command(self, account_id: str, action: str) -> str:
        if self._pool is None:  # pragma: no cover - guarded by _start
            msg = "account pool is unavailable"
            raise RuntimeError(msg)
        account = await domain_accounts.get_account(account_id)
        if account is None:
            msg = f"account no longer exists: {account_id}"
            raise ValueError(msg)
        if action in {"start", "restart"} and not account.enabled:
            msg = f"account disabled before command execution: {account_id}"
            raise ValueError(msg)
        if action in {"start", "restart"}:
            blocked = await worker_risk.start_block_reason(account_id)
            if blocked:
                raise ValueError(blocked)
        if action == "start":
            return self._pool.ensure_started(account_id)
        if action == "stop":
            return await self._pool.ensure_stopped(account_id)
        if action == "restart":
            return await self._pool.restart_worker(account_id)
        msg = f"unsupported worker action: {action}"
        raise ValueError(msg)

    async def _shutdown(self, *, fatal_error: str | None, restart_requested: bool) -> None:
        if self._instance_created:
            with suppress(Exception):
                await daemon_domain.mark_stopping(self.instance_id)
        if self._pool is not None:
            with suppress(Exception):
                await self._pool.stop_all()
        if self._instance_created:
            with suppress(Exception):
                await daemon_domain.mark_stopped(self.instance_id, error=fatal_error)
        self._restore_signal_handlers()
        self._lock.release()
        logger.info(
            "daemon stopped instance=%s restart=%s error=%s",
            self.instance_id,
            restart_requested,
            fatal_error or "-",
        )

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
                self._signal_registrations.append(("loop", sig, loop))
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    previous = signal.getsignal(sig)
                    signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(self.request_stop))
                    self._signal_registrations.append(("signal", sig, previous))
                except (OSError, RuntimeError, ValueError):
                    logger.debug("signal handler unavailable: %s", sig)

    def _restore_signal_handlers(self) -> None:
        for kind, sig, value in reversed(self._signal_registrations):
            with suppress(Exception):
                if kind == "loop":
                    value.remove_signal_handler(sig)
                else:
                    signal.signal(sig, value)
        self._signal_registrations.clear()


async def run_runtime_daemon() -> None:
    """执行 daemon,并在收到 restart 请求后于同一启动进程内重建实例。"""
    while True:
        runtime = RuntimeDaemon()
        if not await runtime.run():
            return
