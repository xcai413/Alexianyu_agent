"""持有账号池生命周期的常驻 RuntimeDaemon。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import timedelta
from functools import partial
from typing import Any

import uvicorn

from xianyu_agent import __version__
from xianyu_agent.application.attention import AlertNotifier, AttentionService
from xianyu_agent.application.automation import PlatformAutomationScheduler
from xianyu_agent.application.delivery.executor import DeliveryExecutor, DeliveryRequest
from xianyu_agent.application.health.http import create_health_app
from xianyu_agent.application.health.soak import sample_active_soaks
from xianyu_agent.application.notify import NotificationDispatcher
from xianyu_agent.application.session.renewal import LongLoginRenewalService
from xianyu_agent.application.session.renewal_schedule import LongLoginRenewalScheduler
from xianyu_agent.config import get_settings
from xianyu_agent.domain.account import risk as worker_risk, state as domain_accounts
from xianyu_agent.domain.runtime import command as worker_commands, daemon as daemon_domain
from xianyu_agent.runtime.account_pool import AccountPool
from xianyu_agent.runtime.daemon_lock import DaemonLock
from xianyu_agent.runtime.delivery_sender import (
    make_consign_port,
    make_pool_image_sender,
    make_pool_sender,
)
from xianyu_agent.services.logging_setup import configure_daemon_logging

logger = logging.getLogger(__name__)
PoolFactory = Callable[[], Awaitable[AccountPool]]


class _EmbeddedUvicornServer(uvicorn.Server):
    """嵌入 daemon 时由外层统一拥有 SIGINT/SIGTERM。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class RuntimeDaemon:
    """单进程管理全部启用账号的长期运行协调器。"""

    def __init__(
        self,
        *,
        heartbeat_interval_s: float = 10.0,
        reconcile_interval_s: float = 5.0,
        renewal_interval_s: float | None = None,
        soak_interval_s: float = 60.0,
        alert_interval_s: float | None = None,
        automation_interval_s: float | None = None,
        pool_factory: PoolFactory | None = None,
        lock: DaemonLock | None = None,
        renewal_scheduler: Any | None = None,
        platform_scheduler: Any | None = None,
        alert_notifier: Any | None = None,
        install_signal_handlers: bool = True,
        configure_logging: bool = True,
    ) -> None:
        settings = get_settings()
        self.instance_id = uuid.uuid4().hex
        self.heartbeat_interval_s = heartbeat_interval_s
        self.reconcile_interval_s = reconcile_interval_s
        self.renewal_enabled = settings.credential_renewal_enabled
        self.renewal_interval_s = (
            renewal_interval_s
            if renewal_interval_s is not None
            else float(settings.credential_renewal_interval_seconds)
        )
        self.renewal_force_interval_s = float(settings.credential_renewal_force_interval_seconds)
        self.soak_interval_s = soak_interval_s
        self.alert_interval_s = (
            alert_interval_s
            if alert_interval_s is not None
            else float(settings.alert_interval_seconds)
        )
        self.automation_interval_s = (
            automation_interval_s
            if automation_interval_s is not None
            else float(settings.platform_automation_interval_seconds)
        )
        self._renewal_service = LongLoginRenewalService()
        self._pool_factory = pool_factory or partial(
            AccountPool.from_desired_accounts,
            instance_id=self.instance_id,
            renewal_service=self._renewal_service,
        )
        self._renewal_scheduler = renewal_scheduler
        self._platform_scheduler = platform_scheduler
        self._alert_notifier = alert_notifier
        self._alert_dispatcher: NotificationDispatcher | None = None
        self._lock = lock or DaemonLock(settings.daemon_lock_path)
        self._install_signals = install_signal_handlers
        self._configure_logging = configure_logging
        self._stop_event = asyncio.Event()
        self._pool: AccountPool | None = None
        self._instance_created = False
        self._signal_registrations: list[tuple[str, signal.Signals, Any]] = []
        self._health_server: uvicorn.Server | None = None
        self._health_task: asyncio.Task[None] | None = None

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
        settings = get_settings()
        orphaned = await daemon_domain.mark_expired_instances(
            reason="previous daemon lease expired without clean shutdown",
            exclude_instance_id=self.instance_id,
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
        binder = getattr(self._pool, "bind_instance", None)
        if callable(binder):
            # 自定义 pool_factory 可能没带实例身份;绑定后账号启动才走归属租约。
            binder(self.instance_id)
        started = self._pool.start_all()
        await daemon_domain.mark_running(self.instance_id)
        if settings.health_server_enabled:
            await self._start_health_server(settings.http_host, settings.http_port)
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
        next_renewal_at = time.monotonic() + self.renewal_interval_s
        next_force_renewal_at = time.monotonic()
        next_soak_at = time.monotonic() + self.soak_interval_s
        next_automation_at = time.monotonic()
        next_alert_at = time.monotonic() + self.alert_interval_s
        while not self._stop_event.is_set():
            if self._health_task is not None and self._health_task.done():
                error = self._health_task.exception()
                msg = "health server stopped unexpectedly"
                if error is not None:
                    raise RuntimeError(msg) from error
                raise RuntimeError(msg)
            control = await daemon_domain.touch_heartbeat(self.instance_id)
            if control.shutdown_requested:
                self.request_stop()
                return control.restart_requested
            await self._process_worker_commands()
            renew = getattr(self._pool, "renew_owned_leases", None)
            if callable(renew):
                await renew()
            now = time.monotonic()
            if now >= next_reconcile_at:
                await self._reconcile()
                next_reconcile_at = now + self.reconcile_interval_s
            if self.renewal_enabled and now >= next_renewal_at:
                force = now >= next_force_renewal_at
                await self._renew_credentials(force=force)
                next_renewal_at = now + self.renewal_interval_s
                if force:
                    next_force_renewal_at = now + self.renewal_force_interval_s
            if now >= next_soak_at:
                await self._sample_soaks()
                next_soak_at = now + self.soak_interval_s
            if now >= next_automation_at:
                await self._run_platform_automation()
                next_automation_at = now + self.automation_interval_s
            if now >= next_alert_at:
                await self._dispatch_alerts()
                next_alert_at = now + self.alert_interval_s
            timeout = min(
                self.heartbeat_interval_s,
                self.reconcile_interval_s,
                self.renewal_interval_s if self.renewal_enabled else self.heartbeat_interval_s,
                self.soak_interval_s,
                self.automation_interval_s,
                self.alert_interval_s,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        return False

    async def _sample_soaks(self) -> None:
        """参与集群单选采样;采样故障不得终止 daemon。"""
        try:
            await sample_active_soaks(sampler_id=self.instance_id)
        except Exception as exc:
            logger.warning("soak sampling pass failed: %s", type(exc).__name__)

    async def _dispatch_alerts(self) -> None:
        """把新出现的待人工处理事项推给运维配置的渠道;没配任何渠道就是空操作。

        优先级:数据库里启用的通知渠道 → 环境变量里的单个 webhook。两条路都靠 `alert_dispatches`
        的唯一键收敛,所以同一事项不会因为换了路径而重复发送。
        """
        try:
            attention = AttentionService()
            items = await attention.list_items()
            message_notifications = await attention.list_message_notifications()
            if self._alert_dispatcher is None:
                self._alert_dispatcher = NotificationDispatcher(
                    timeout_seconds=get_settings().alert_webhook_timeout_seconds
                )
            # 消息通知只走已配置的通知渠道;环境变量兜底 webhook 只接收人工待办。
            result = await self._alert_dispatcher.dispatch((*items, *message_notifications))
            if result.enabled:
                return
            notifier = self._alert_notifier or self._build_alert_notifier()
            if not notifier.enabled:
                return
            await notifier.dispatch(items)
        except Exception as exc:
            # 通知是辅助能力:任何失败都不允许影响 daemon 主循环。
            logger.warning("alert dispatch pass failed: %s", type(exc).__name__)

    def _build_alert_notifier(self) -> AlertNotifier:
        settings = get_settings()
        self._alert_notifier = AlertNotifier(
            url=settings.alert_webhook_url,
            timeout_seconds=settings.alert_webhook_timeout_seconds,
        )
        return self._alert_notifier

    async def _run_platform_automation(self) -> None:
        """Run one bounded platform-automation pass without risking daemon life."""
        try:
            scheduler = self._platform_scheduler or self._build_platform_scheduler()
            result = await scheduler.run_once()
        except Exception as exc:
            logger.warning("platform automation pass failed: %s", type(exc).__name__)
            return
        if result.seeded or result.recovered or result.runner.claimed:
            logger.info(
                "platform automation pass seeded=%d recovered=%d claimed=%d completed=%d "
                "released=%d stale=%d",
                result.seeded,
                result.recovered,
                result.runner.claimed,
                result.runner.completed,
                result.runner.released,
                result.runner.stale,
            )

    def _build_platform_scheduler(self):
        settings = get_settings()
        self._platform_scheduler = PlatformAutomationScheduler(
            instance_id=self.instance_id,
            active=settings.automation_mode == "active",
            timezone=settings.tz,
        )
        return self._platform_scheduler

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

    async def _renew_credentials(self, *, force: bool = False) -> None:
        """Run the long-login renewal pass; never let it end the daemon.

        Renewal is session maintenance for unattended operation: it runs for every
        account that is expected online, respects the risk circuit inside the
        service, and a failure here is logged instead of killing the loop.
        """
        try:
            scheduler = self._renewal_scheduler or self._build_renewal_scheduler()
            if scheduler is None:
                return
            reports = await scheduler.run_once(force=True) if force else await scheduler.run_once()
        except Exception as exc:
            logger.warning("credential renewal pass failed: %s", type(exc).__name__)
            return
        renewed = [report.account_id for report in reports if report.success]
        needs_human = [report.account_id for report in reports if report.need_human_login]
        if renewed:
            logger.info("credential renewal succeeded for %d account(s)", len(renewed))
        if needs_human:
            logger.warning(
                "credential renewal needs human action for %d account(s)", len(needs_human)
            )

    def _build_renewal_scheduler(self):
        """Build the default scheduler from settings; injectable for tests."""
        setup = get_settings()

        async def list_accounts() -> Sequence[str]:
            records = await domain_accounts.list_desired_running_accounts()
            return [record.account_id for record in records]

        self._renewal_scheduler = LongLoginRenewalScheduler(
            account_lister=list_accounts,
            lead_time=timedelta(hours=setup.credential_renewal_lead_hours),
            service=self._renewal_service,
        )
        return self._renewal_scheduler

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
                result = await self._execute_worker_command(
                    account_id,
                    command.action,
                    payload=command.payload,
                )
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

    async def _execute_worker_command(
        self, account_id: str, action: str, *, payload: dict | None = None
    ) -> str:
        if self._pool is None:  # pragma: no cover - guarded by _start
            msg = "account pool is unavailable"
            raise RuntimeError(msg)
        account = await domain_accounts.get_account(account_id)
        if account is None:
            msg = f"account no longer exists: {account_id}"
            raise ValueError(msg)
        await self._validate_worker_command(account_id, action, enabled=account.enabled)
        if action == "start":
            result = await self._start_worker_command(account_id)
        elif action == "stop":
            result = await self._stop_worker_command(account_id)
        elif action == "restart":
            result = await self._restart_worker_command(account_id)
        elif action == worker_commands.SEND_REPLY_ACTION:
            result = await self._execute_send_reply(account_id, payload)
        elif action == worker_commands.DELIVER_ACTION:
            result = await self._execute_delivery(account_id, payload)
        else:
            msg = f"unsupported worker action: {action}"
            raise ValueError(msg)
        return result

    async def _validate_worker_command(self, account_id: str, action: str, *, enabled: bool) -> None:
        """Reject a command that must not run for this account's current state."""
        needs_enabled = {
            "start",
            "restart",
            worker_commands.SEND_REPLY_ACTION,
            worker_commands.DELIVER_ACTION,
        }
        if action in needs_enabled and not enabled:
            msg = f"account disabled before command execution: {account_id}"
            raise ValueError(msg)
        if action in {"start", "restart"}:
            blocked = await worker_risk.start_block_reason(account_id)
            if blocked:
                raise ValueError(blocked)

    async def _start_worker_command(self, account_id: str) -> str:
        assert self._pool is not None  # 由 _execute_worker_command 保证
        starter = getattr(self._pool, "ensure_started_owned", None)
        if callable(starter):
            return await starter(account_id)
        return self._pool.ensure_started(account_id)

    async def _stop_worker_command(self, account_id: str) -> str:
        assert self._pool is not None  # 由 _execute_worker_command 保证
        result = await self._pool.ensure_stopped(account_id)
        releaser = getattr(self._pool, "release_lease", None)
        if callable(releaser):
            await releaser(account_id)
        return result

    async def _restart_worker_command(self, account_id: str) -> str:
        assert self._pool is not None  # 由 _execute_worker_command 保证
        restarter = getattr(self._pool, "restart_worker_owned", None)
        if callable(restarter):
            return await restarter(account_id)
        return await self._pool.restart_worker(account_id)

    async def _execute_send_reply(self, account_id: str, payload: dict | None) -> str:
        """Validate an operator-reply payload and hand it to the owning Worker."""
        if self._pool is None:  # pragma: no cover - guarded by _start
            msg = "account pool is unavailable"
            raise RuntimeError(msg)
        chat_id = (payload or {}).get("chat_id")
        text = (payload or {}).get("text")
        if not isinstance(chat_id, str) or not chat_id:
            msg = "send_reply payload missing chat_id"
            raise ValueError(msg)
        if not isinstance(text, str) or not text.strip():
            msg = "send_reply payload missing text"
            raise ValueError(msg)
        sender = getattr(self._pool, "send_operator_reply", None)
        if not callable(sender):
            msg = "account pool does not support operator replies"
            raise TypeError(msg)
        return await sender(account_id, chat_id=chat_id, text=text)

    async def _execute_delivery(self, account_id: str, payload: dict | None) -> str:
        """Run the phase-6 executor for one order using the live Worker as sender."""
        if self._pool is None:  # pragma: no cover - guarded by _start
            msg = "account pool is unavailable"
            raise RuntimeError(msg)
        order_id = (payload or {}).get("order_id")
        if not isinstance(order_id, str) or not order_id:
            msg = "deliver payload missing order_id"
            raise ValueError(msg)
        item_id = (payload or {}).get("item_id")
        quantity = (payload or {}).get("quantity")
        result = await DeliveryExecutor(
            sender=make_pool_sender(self._pool),
            image_sender=make_pool_image_sender(self._pool),
            consign=make_consign_port(),
        ).execute(
            DeliveryRequest(
                account_id=account_id,
                order_id=order_id,
                item_id=item_id if isinstance(item_id, str) and item_id else None,
                quantity=quantity if isinstance(quantity, int) and quantity > 0 else None,
            )
        )
        return f"{result.decision}:{result.job.state}"

    async def _shutdown(self, *, fatal_error: str | None, restart_requested: bool) -> None:
        if self._instance_created:
            with suppress(Exception):
                await daemon_domain.mark_stopping(self.instance_id)
        if self._pool is not None:
            with suppress(Exception):
                await self._pool.stop_all()
            releaser = getattr(self._pool, "release_all_leases", None)
            if callable(releaser):
                with suppress(Exception):
                    await releaser()
        if self._instance_created:
            with suppress(Exception):
                await daemon_domain.mark_stopped(self.instance_id, error=fatal_error)
        await self._stop_health_server()
        self._restore_signal_handlers()
        self._lock.release()
        logger.info(
            "daemon stopped instance=%s restart=%s error=%s",
            self.instance_id,
            restart_requested,
            fatal_error or "-",
        )

    async def _start_health_server(self, host: str, port: int) -> None:
        config = uvicorn.Config(
            create_health_app(self.instance_id),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._health_server = _EmbeddedUvicornServer(config)
        self._health_task = asyncio.create_task(
            self._health_server.serve(), name=f"health-server-{self.instance_id}"
        )
        for _ in range(500):
            if self._health_server.started:
                return
            if self._health_task.done():
                await self._health_task
                msg = "health server exited before startup"
                raise RuntimeError(msg)
            await asyncio.sleep(0.01)
        msg = f"health server startup timeout: {host}:{port}"
        raise RuntimeError(msg)

    async def _stop_health_server(self) -> None:
        if self._health_server is not None:
            self._health_server.should_exit = True
        if self._health_task is not None:
            with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._health_task, timeout=5.0)
        self._health_server = None
        self._health_task = None

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
