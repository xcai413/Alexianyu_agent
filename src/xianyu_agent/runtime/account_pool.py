"""AccountPool: manages N AccountWorkers for the runtime daemon.

Workers are built from enabled accounts in the DB and started together (or
individually). Heartbeats are persisted to the worker_status table by each
WsClient, so status is queryable from any process.

P0.2: RuntimeDaemon owns this pool. CLI writes worker_commands and desired_state;
the daemon reconciles this in-memory pool against that persistent control plane.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from xianyu_agent.application.session.renewal import LongLoginRenewalService
from xianyu_agent.config import get_settings
from xianyu_agent.domain.account import lease as account_lease, state as domain_accounts
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.protocol.events import ConnectionState, EventEnvelope
from xianyu_agent.runtime.account_lock import AccountConnectionAlreadyRunningError
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.runtime.message_sender import send_worker_text
from xianyu_agent.utils.time_utils import format_local

EventHandler = Callable[[EventEnvelope], Awaitable[None]]
WorkerFactory = Callable[[str], AccountWorker]
RetirementCleanup = Callable[[], Awaitable[None]]
logger = logging.getLogger(__name__)


class WorkerNotReadyError(RuntimeError):
    """发送前不可用:账号没有在线 Worker,或它在就绪前失败/需要人工验证。

    这个异常只表示「消息还没写出去」,因此调用方可以安全地按可重试处理;
    发送调用本身抛出的异常不属于这一类,必须按结果不明处理。
    """


class AccountPool:
    def __init__(
        self,
        *,
        worker_factory: WorkerFactory | None = None,
        instance_id: str | None = None,
        takeover_require_approval: bool | None = None,
        takeover_max_failures: int | None = None,
        renewal_service: LongLoginRenewalService | None = None,
    ) -> None:
        settings = get_settings()
        self._workers: dict[str, AccountWorker] = {}
        self._worker_factory = worker_factory or AccountWorker
        self._renewal_service = renewal_service
        # 绑定副本身份后,账号启动与对账都走租约(M2);为空保持单机行为。
        self._instance_id = instance_id
        self._takeover_require_approval = (
            settings.takeover_require_approval
            if takeover_require_approval is None
            else takeover_require_approval
        )
        self._takeover_max_failures = (
            settings.takeover_max_failures
            if takeover_max_failures is None
            else takeover_max_failures
        )
        self._claimed_takeovers: dict[str, bool] = {}
        self._takeover_result_tasks: set[asyncio.Task[object]] = set()
        self._startup_tasks: dict[str, asyncio.Task[None]] = {}
        self._transport_tasks: dict[str, asyncio.Task[None]] = {}
        # Compatibility/inspection set for lifecycle-triggered retirements.  The
        # exact same Task is also the canonical per-account retirement barrier.
        self._lifecycle_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._retirement_tasks: dict[str, asyncio.Task[None]] = {}

    def bind_instance(self, instance_id: str) -> None:
        """绑定副本身份;绑定后账号启动与对账按归属租约分片(M2)。"""
        self._instance_id = instance_id

    @property
    def instance_id(self) -> str | None:
        return self._instance_id

    async def renew_owned_leases(self) -> int:
        """续租本实例持有的账号;单机模式(无身份)返回 0。"""
        if self._instance_id is None:
            return 0
        return await account_lease.renew_owned(self._instance_id)

    async def release_lease(self, account_id: str) -> bool:
        if self._instance_id is None:
            return False
        return await account_lease.release(account_id, instance_id=self._instance_id)

    async def release_all_leases(self) -> int:
        if self._instance_id is None:
            return 0
        return await account_lease.release_owned(self._instance_id)

    async def ensure_started_owned(self, account_id: str) -> str:
        """按归属租约启动账号;抢不到租约时明确返回,不启动 Worker。"""
        takeover = False
        if self._instance_id is not None:
            claim = await account_lease.claim_for_start(
                account_id,
                instance_id=self._instance_id,
                require_approval=self._takeover_require_approval,
            )
            if not claim.acquired:
                return claim.reason
            takeover = claim.takeover
        return self.ensure_started(account_id, takeover=takeover)

    @classmethod
    async def from_enabled_accounts(
        cls,
        *,
        on_event: EventHandler | None = None,
        renewal_service: LongLoginRenewalService | None = None,
    ) -> AccountPool:
        """Build a pool with one worker per enabled account."""

        def _build_worker(account_id: str) -> AccountWorker:
            worker = AccountWorker(
                account_id,
                persist_events=on_event is None,
                renewal_service=renewal_service,
            )
            if on_event is not None:
                worker._client.on_event = on_event
            return worker

        pool = cls(worker_factory=_build_worker)
        accounts = await domain_accounts.list_accounts(only_enabled=True)
        for acc in accounts:
            pool._workers[acc.account_id] = pool._worker_factory(acc.account_id)
        return pool

    @classmethod
    async def from_desired_accounts(
        cls,
        *,
        on_event: EventHandler | None = None,
        instance_id: str | None = None,
        renewal_service: LongLoginRenewalService | None = None,
    ) -> AccountPool:
        """Build a pool from enabled accounts whose desired_state is running."""

        def _build_worker(account_id: str) -> AccountWorker:
            worker = AccountWorker(
                account_id,
                persist_events=on_event is None,
                renewal_service=renewal_service,
            )
            if on_event is not None:
                worker._client.on_event = on_event
            return worker

        pool = cls(worker_factory=_build_worker, instance_id=instance_id)
        accounts = await domain_accounts.list_desired_running_accounts()
        for acc in accounts:
            takeover = False
            if instance_id is not None:
                claim = await account_lease.claim_for_start(
                    acc.account_id,
                    instance_id=instance_id,
                    require_approval=pool._takeover_require_approval,
                )
                if not claim.acquired:
                    logger.info(
                        "account lease unavailable account=%s reason=%s",
                        acc.account_id,
                        claim.reason,
                    )
                    continue
                takeover = claim.takeover
            pool._workers[acc.account_id] = pool._worker_factory(acc.account_id)
            pool._claimed_takeovers[acc.account_id] = takeover
        return pool

    def _schedule_takeover_result(
        self,
        account_id: str,
        *,
        succeeded: bool,
        error_type: str = "-",
    ) -> None:
        """持久化接管启动结果;任务归 Pool 所有,停机时会统一排空。"""
        if self._instance_id is None:
            return

        async def _record() -> object:
            if succeeded:
                return await account_lease.record_takeover_success(
                    account_id, instance_id=self._instance_id or ""
                )
            return await account_lease.record_takeover_failure(
                account_id,
                instance_id=self._instance_id or "",
                max_failures=self._takeover_max_failures,
                error_type=error_type,
            )

        task = asyncio.create_task(_record(), name=f"takeover-result-{account_id}")
        self._takeover_result_tasks.add(task)

        def _completed(completed: asyncio.Task[object]) -> None:
            self._takeover_result_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if isinstance(error, Exception):
                logger.error(
                    "account takeover result persistence failed account=%s",
                    account_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_completed)

    async def _drain_takeover_result_tasks(self) -> None:
        while self._takeover_result_tasks:
            tasks = tuple(self._takeover_result_tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error(
                        "account takeover result persistence failed",
                        exc_info=(type(result), result, result.__traceback__),
                    )

    def _observe_startup(
        self,
        account_id: str,
        worker: AccountWorker,
        task: asyncio.Task[None] | None,
        *,
        takeover: bool = False,
    ) -> None:
        """Own a scheduled startup outcome for synchronous pool entry points."""
        if task is None:
            if takeover:
                self._schedule_takeover_result(
                    account_id,
                    succeeded=False,
                    error_type="StartupNotScheduled",
                )
            self._observe_transport_task(account_id, worker)
            return
        self._startup_tasks[account_id] = task

        def _completed(completed: asyncio.Task[None]) -> None:
            if self._startup_tasks.get(account_id) is completed:
                self._startup_tasks.pop(account_id, None)
            if completed.cancelled():
                return
            error = completed.exception()
            is_current = self._workers.get(account_id) is worker
            if error is None and worker.state not in {
                WorkerState.DISABLED,
                WorkerState.ERROR,
                WorkerState.NEEDS_VALIDATION,
            }:
                if takeover and is_current:
                    self._schedule_takeover_result(account_id, succeeded=True)
                self._observe_transport_task(account_id, worker)
                return
            if is_current:
                self._workers.pop(account_id, None)
                if takeover:
                    self._schedule_takeover_result(
                        account_id,
                        succeeded=False,
                        error_type=type(error).__name__
                        if error is not None
                        else "WorkerStateError",
                    )
            if error is None:
                logger.error(
                    "account worker startup ended in terminal state account=%s state=%s",
                    account_id,
                    worker.state.value,
                )
            else:
                logger.error(
                    "account worker startup failed account=%s error=%s",
                    account_id,
                    error,
                )

        task.add_done_callback(_completed)

    def _observe_transport_task(self, account_id: str, worker: AccountWorker) -> None:
        """Own transport completion so dead workers cannot remain current forever."""
        task = cast(
            asyncio.Task[None] | None,
            getattr(getattr(worker, "_client", None), "_task", None),
        )
        if task is None or self._transport_tasks.get(account_id) is task:
            return
        self._transport_tasks[account_id] = task

        def _completed(completed: asyncio.Task[None]) -> None:
            if self._transport_tasks.get(account_id) is completed:
                self._transport_tasks.pop(account_id, None)
            if completed.cancelled():
                return
            error = completed.exception()
            lifecycle_error = worker.lifecycle_error
            cleanup: asyncio.Task[None] | None = None
            if isinstance(lifecycle_error, Exception):
                cleanup = self._schedule_failed_transport_retirement(
                    account_id,
                    worker,
                    lifecycle_error,
                )
            elif isinstance(error, Exception):
                cleanup = self._schedule_failed_transport_retirement(
                    account_id,
                    worker,
                    error,
                )
            elif not worker._stop_requested and worker.state is WorkerState.ERROR:
                cleanup = self._schedule_terminal_transport_retirement(account_id, worker)
            if cleanup is None:
                return
            self._lifecycle_cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._lifecycle_cleanup_tasks.discard)

        task.add_done_callback(_completed)

    def _schedule_worker_retirement(
        self,
        account_id: str,
        worker: AccountWorker,
        cleanup: RetirementCleanup,
        *,
        task_name: str,
    ) -> asyncio.Task[None] | None:
        """Atomically claim one generation and make its Pool-owned cleanup the barrier."""
        if account_id in self._retirement_tasks:
            return None
        if self._workers.get(account_id) is not worker:
            return None

        # Claim synchronously, before the first cleanup await.  Once removed,
        # no competing path may physically clean this exact worker again.
        self._workers.pop(account_id, None)

        async def _run_cleanup() -> None:
            await cleanup()

        try:
            task = asyncio.create_task(_run_cleanup(), name=task_name)
        except BaseException:
            # Task construction has no await; restoring this exact generation is
            # safe because no competing event-loop callback could have run.
            if account_id not in self._workers:
                self._workers[account_id] = worker
            raise
        self._retirement_tasks[account_id] = task

        def _completed(completed: asyncio.Task[None]) -> None:
            # The task itself is the generation barrier.  It disappears only
            # after the physical cleanup coroutine has actually terminated.
            if self._retirement_tasks.get(account_id) is completed:
                self._retirement_tasks.pop(account_id, None)
            if completed.cancelled():
                logger.error("account worker retirement task cancelled account=%s", account_id)
                return
            error = completed.exception()
            if isinstance(error, Exception):
                logger.error(
                    "account worker retirement cleanup failed account=%s error=%s",
                    account_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_completed)
        return task

    def _schedule_stop_retirement(
        self,
        account_id: str,
        worker: AccountWorker,
        *,
        reason: str,
    ) -> asyncio.Task[None] | None:
        return self._schedule_worker_retirement(
            account_id,
            worker,
            worker.stop,
            task_name=f"worker-retirement-{reason}-{account_id}",
        )

    def _schedule_failed_transport_retirement(
        self,
        account_id: str,
        worker: AccountWorker,
        error: Exception,
    ) -> asyncio.Task[None] | None:
        async def _cleanup() -> None:
            try:
                await worker._fail_closed_transport_after_lifecycle_error(error)
            except Exception:
                logger.exception(
                    "account worker lifecycle fail-closed cleanup failed account=%s",
                    account_id,
                )
            finally:
                logger.error(
                    "account worker transport lifecycle failed account=%s error=%s",
                    account_id,
                    error,
                )

        return self._schedule_worker_retirement(
            account_id,
            worker,
            _cleanup,
            task_name=f"worker-lifecycle-failure-{account_id}",
        )

    def _schedule_terminal_transport_retirement(
        self,
        account_id: str,
        worker: AccountWorker,
    ) -> asyncio.Task[None] | None:
        async def _cleanup() -> None:
            try:
                await worker.stop()
            except Exception:
                logger.exception(
                    "account worker terminal transport cleanup failed account=%s",
                    account_id,
                )
            finally:
                logger.error(
                    "account worker transport ended in terminal state account=%s state=%s",
                    account_id,
                    worker.state.value,
                )

        return self._schedule_worker_retirement(
            account_id,
            worker,
            _cleanup,
            task_name=f"worker-terminal-transport-{account_id}",
        )

    async def _await_worker_retirement(self, account_id: str) -> None:
        """Wait only for this account's Pool-owned retirement, isolated from cancellation."""
        while True:
            task = self._retirement_tasks.get(account_id)
            if task is None:
                return
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    if self._retirement_tasks.get(account_id) is task:
                        self._retirement_tasks.pop(account_id, None)
                    continue
                raise
            except Exception:
                # The retirement task completion callback records the cleanup
                # failure.  A waiter that did not originate the cleanup observes
                # barrier completion rather than stealing its error ownership.
                pass
            finally:
                if task.done() and self._retirement_tasks.get(account_id) is task:
                    self._retirement_tasks.pop(account_id, None)

    async def _drain_observed_transport_tasks(self) -> None:
        """Drain observed transports so their completion callbacks can register cleanup."""
        while self._transport_tasks:
            tasks = tuple(self._transport_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drain_lifecycle_cleanup_tasks(self) -> None:
        """Drain lifecycle observers without transferring cancellation to retirement tasks."""
        while self._lifecycle_cleanup_tasks:
            tasks = tuple(self._lifecycle_cleanup_tasks)
            results = await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.error(
                        "account worker lifecycle cleanup task failed",
                        exc_info=(type(result), result, result.__traceback__),
                    )

    async def _drain_retirement_tasks(self) -> list[Exception]:
        """Drain every Pool-owned retirement task, including tasks added while draining."""
        errors: list[Exception] = []
        while self._retirement_tasks:
            tasks = tuple(self._retirement_tasks.values())
            results = await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )
            errors.extend(result for result in results if isinstance(result, Exception))
            # Done callbacks normally remove these entries.  Remove any done
            # leftovers synchronously so an already-complete task cannot keep a
            # drain loop alive merely because its callback has not run yet.
            for account_id, task in list(self._retirement_tasks.items()):
                if task.done() and self._retirement_tasks.get(account_id) is task:
                    self._retirement_tasks.pop(account_id, None)
        return errors

    async def _retire_failed_transport(
        self,
        account_id: str,
        worker: AccountWorker,
        error: Exception,
    ) -> None:
        """Compatibility waiter around the canonical Pool-owned failure retirement."""
        task = self._schedule_failed_transport_retirement(account_id, worker, error)
        if task is None:
            return
        await asyncio.shield(task)

    async def _retire_terminal_transport(
        self,
        account_id: str,
        worker: AccountWorker,
    ) -> None:
        """Compatibility waiter around the canonical Pool-owned terminal retirement."""
        task = self._schedule_terminal_transport_retirement(account_id, worker)
        if task is None:
            return
        await asyncio.shield(task)

    def _start_observed(
        self,
        account_id: str,
        worker: AccountWorker,
        *,
        takeover: bool = False,
    ) -> asyncio.Task[None] | None:
        try:
            task = worker.start()
        except Exception as exc:
            if takeover:
                self._schedule_takeover_result(
                    account_id,
                    succeeded=False,
                    error_type=type(exc).__name__,
                )
            raise
        self._observe_startup(account_id, worker, task, takeover=takeover)
        return task

    @property
    def account_ids(self) -> list[str]:
        return list(self._workers)

    def has(self, account_id: str) -> bool:
        return account_id in self._workers

    def get(self, account_id: str) -> AccountWorker | None:
        return self._workers.get(account_id)

    async def send_operator_reply(
        self,
        account_id: str,
        *,
        chat_id: str,
        text: str,
        timeout_s: float = 15.0,
    ) -> str:
        """Send one operator-authored reply through the Worker that owns the account.

        Reuses the canonical outbound path (SendMessageService + audit recorder), so
        an operator reply produces the same attempt/result evidence as an automated
        one. Never starts, restarts, or reconnects the Worker.
        """
        worker = await self.require_ready_worker(account_id, timeout_s=timeout_s)
        sent = await send_worker_text(
            worker._client,
            account_id=account_id,
            chat_id=chat_id,
            text=text,
        )
        if not sent:
            msg = f"账号 {account_id} 会话 {chat_id} 外发未确认成功"
            raise ValueError(msg)
        return "sent"

    async def require_ready_worker(
        self,
        account_id: str,
        *,
        timeout_s: float = 15.0,
    ) -> AccountWorker:
        """Return the live Worker only when it can accept an outbound message.

        Raises WorkerNotReadyError for every pre-send condition, so callers can
        tell "nothing was written" apart from "unknown result".
        """
        worker = self._workers.get(account_id)
        if worker is None:
            msg = f"账号 {account_id} 当前没有运行中的 Worker,无法外发"
            raise WorkerNotReadyError(msg)
        try:
            await self._wait_for_protocol_ready(worker, timeout_s=timeout_s)
        except (ValueError, TimeoutError) as exc:
            raise WorkerNotReadyError(str(exc)) from exc
        return worker

    async def _wait_for_protocol_ready(
        self,
        worker: AccountWorker,
        *,
        timeout_s: float,
        poll_s: float = 0.05,
    ) -> None:
        """Wait for a business-ready session, or raise before any send happens."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        while True:
            if worker.state is WorkerState.NEEDS_VALIDATION:
                msg = "账号需要人工验证,已拒绝外发"
                raise ValueError(msg)
            if worker.state is WorkerState.ERROR:
                msg = "账号 Worker 处于 ERROR,已拒绝外发"
                raise ValueError(msg)
            if worker._client.state is ConnectionState.CONNECTED:
                return
            if loop.time() >= deadline:
                msg = "等待 WebSocket 协议会话就绪超时"
                raise TimeoutError(msg)
            await asyncio.sleep(poll_s)

    def start(self, account_id: str) -> bool:
        """Start one worker. Returns False if account is unknown or retiring."""
        if account_id in self._retirement_tasks:
            return False
        worker = self._workers.get(account_id)
        if worker is None:
            return False
        self._start_observed(account_id, worker)
        return True

    def start_all(self) -> list[str]:
        started: list[str] = []
        for account_id in list(self._workers):
            if account_id in self._retirement_tasks:
                continue
            worker = self._workers.get(account_id)
            if worker is None:
                continue
            takeover = self._claimed_takeovers.pop(account_id, False)
            try:
                self._start_observed(account_id, worker, takeover=takeover)
            except AccountConnectionAlreadyRunningError:
                if self._workers.get(account_id) is worker:
                    self._workers.pop(account_id, None)
                logger.warning("account connection lock busy account=%s", account_id)
            else:
                started.append(account_id)
        return started

    async def stop(self, account_id: str) -> bool:
        worker = self._workers.get(account_id)
        if worker is None:
            return False
        await worker.stop()
        return True

    async def stop_all(self) -> None:
        # Claim the entire current snapshot before yielding.  Each physical stop
        # is then owned by an independent per-account task, so one slow account
        # cannot prevent other cleanup tasks from starting.
        for account_id, worker in list(self._workers.items()):
            self._schedule_stop_retirement(account_id, worker, reason="shutdown")

        errors = await self._drain_retirement_tasks()
        await self._drain_observed_transport_tasks()
        await self._drain_lifecycle_cleanup_tasks()
        # Transport completion callbacks can register retirement work while the
        # transports are being drained; converge that final generation too.
        errors.extend(await self._drain_retirement_tasks())
        await self._drain_takeover_result_tasks()

        if errors:
            raise errors[0]

    async def reconcile_desired_accounts(self) -> dict[str, list[str]]:  # noqa: PLR0912
        """令内存 Worker 集合与持久化期望状态保持一致。"""
        accounts = await domain_accounts.list_desired_running_accounts()
        desired = {account.account_id for account in accounts}

        # Retirement is scheduled, not awaited: account A's slow physical
        # cleanup may block only account A's next generation, never B/C.
        stopped: list[str] = []
        for account_id, worker in sorted(self._workers.items()):
            keep = account_id in desired
            if keep and self._instance_id is not None:
                # 手里有 Worker 的账号必须仍归我持有;租约丢了就退休,别继续占用连接。
                keep = await account_lease.renew(account_id, instance_id=self._instance_id)
                if not keep:
                    logger.warning("account lease lost, retiring worker account=%s", account_id)
            if keep:
                continue
            task = self._schedule_stop_retirement(account_id, worker, reason="reconcile")
            if task is not None:
                stopped.append(account_id)
                await self.release_lease(account_id)

        # Re-evaluate live ownership instead of using the pre-retirement
        # snapshot.  A transport callback may have retired a desired worker;
        # replacement is eligible only after that account's task is gone.
        started: list[str] = []
        for account_id in sorted(desired):
            if account_id in self._workers or account_id in self._retirement_tasks:
                continue
            takeover = False
            if self._instance_id is not None:
                claim = await account_lease.claim_for_start(
                    account_id,
                    instance_id=self._instance_id,
                    require_approval=self._takeover_require_approval,
                )
                if not claim.acquired:
                    logger.info(
                        "account lease unavailable account=%s reason=%s",
                        account_id,
                        claim.reason,
                    )
                    continue
                takeover = claim.takeover
            worker = self._worker_factory(account_id)
            self._workers[account_id] = worker
            try:
                self._start_observed(account_id, worker, takeover=takeover)
            except AccountConnectionAlreadyRunningError:
                if self._workers.get(account_id) is worker:
                    self._workers.pop(account_id, None)
                logger.warning("account connection lock busy account=%s", account_id)
            except Exception:
                if self._workers.get(account_id) is worker:
                    self._workers.pop(account_id, None)
                logger.exception("account worker startup failed account=%s", account_id)
            else:
                started.append(account_id)
        return {"started": started, "stopped": stopped}

    async def reconcile_enabled_accounts(self) -> dict[str, list[str]]:
        """兼容旧调用;P0.2 起实际按 enabled + desired_state 对账。"""
        return await self.reconcile_desired_accounts()

    def ensure_started(self, account_id: str, *, takeover: bool = False) -> str:
        """确保一个 Worker 在池中运行。"""
        if account_id in self._retirement_tasks:
            return "retiring"
        worker = self._workers.get(account_id)
        if worker is not None:
            self._start_observed(account_id, worker, takeover=takeover)
            return "already_running"
        worker = self._worker_factory(account_id)
        self._workers[account_id] = worker
        try:
            self._start_observed(account_id, worker, takeover=takeover)
        except Exception:
            if self._workers.get(account_id) is worker:
                self._workers.pop(account_id, None)
            raise
        return "started"

    async def ensure_stopped(self, account_id: str) -> str:
        """确保一个 Worker 已从池中停止并移除。"""
        existing = self._retirement_tasks.get(account_id)
        if existing is not None:
            await self._await_worker_retirement(account_id)
            return "already_stopped"
        worker = self._workers.get(account_id)
        if worker is None:
            return "already_stopped"
        task = self._schedule_stop_retirement(account_id, worker, reason="ensure-stopped")
        if task is None:
            await self._await_worker_retirement(account_id)
            return "already_stopped"
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._retirement_tasks.get(account_id) is task:
                self._retirement_tasks.pop(account_id, None)
        return "stopped"

    async def restart_worker_owned(self, account_id: str) -> str:
        """在租约门禁内重建 Worker;非 owner 不得借 restart 建第二条连接。"""
        takeover = False
        if self._instance_id is not None:
            claim = await account_lease.claim_for_start(
                account_id,
                instance_id=self._instance_id,
                require_approval=self._takeover_require_approval,
            )
            if not claim.acquired:
                return claim.reason
            takeover = claim.takeover
        return await self.restart_worker(account_id, takeover=takeover)

    async def restart_worker(self, account_id: str, *, takeover: bool = False) -> str:
        """重建一个账号的 Worker,避免复用已停止 client 的瞬时状态。"""
        # Wait only for this account's existing generation.  Shielding ensures
        # cancellation of the command caller never owns/cancels physical cleanup.
        await self._await_worker_retirement(account_id)
        previous = self._workers.get(account_id)
        if previous is not None:
            task = self._schedule_stop_retirement(account_id, previous, reason="restart")
            if task is None:
                await self._await_worker_retirement(account_id)
            else:
                try:
                    await asyncio.shield(task)
                finally:
                    if task.done() and self._retirement_tasks.get(account_id) is task:
                        self._retirement_tasks.pop(account_id, None)
        await self._await_worker_retirement(account_id)
        worker = self._worker_factory(account_id)
        self._workers[account_id] = worker
        try:
            # Startup remains under the single existing Pool-observed ownership
            # mechanism; restart never awaits credential retry/backoff.
            self._start_observed(account_id, worker, takeover=takeover)
        except Exception:
            if self._workers.get(account_id) is worker:
                self._workers.pop(account_id, None)
            raise
        return "restarted"

    def restart(self, account_id: str) -> bool:
        if account_id in self._retirement_tasks:
            return False
        worker = self._workers.get(account_id)
        if worker is None:
            return False
        self._start_observed(account_id, worker)
        return True

    async def status(self) -> list[dict]:
        """Merge in-memory worker state with persisted worker_status rows."""
        accounts = await domain_accounts.list_accounts()
        id_to_str = {acc.id: acc.account_id for acc in accounts}
        rows = await domain_accounts.worker_statuses()
        by_account: dict[str, dict] = {}
        for row in rows:
            key = id_to_str.get(row.account_id)
            if key is None:
                continue  # pragma: no cover - orphan row guarded by FK
            by_account[key] = {
                "status": row.status,
                "reconnect_attempts": row.reconnect_attempts,
                "last_heartbeat_at": format_local(row.last_heartbeat_at) or None,
                "last_error": row.last_error,
                "started_at": format_local(row.started_at) or None,
                "risk_code": row.risk_code,
                "risk_cooldown_until": format_local(row.risk_cooldown_until) or None,
                "risk_recovery_required": row.risk_recovery_required,
            }
        result: list[dict] = []
        for acc in accounts:
            worker = self._workers.get(acc.account_id)
            status_data = by_account.get(acc.account_id) or {}
            result.append(
                {
                    "account_id": acc.account_id,
                    "enabled": acc.enabled,
                    "desired_state": acc.desired_state,
                    "worker_state": worker.state.value if worker else "no_worker",
                    "db_status": status_data.get("status", "offline"),
                    "reconnect_attempts": status_data.get("reconnect_attempts", 0),
                    "last_heartbeat_at": status_data.get("last_heartbeat_at"),
                    "last_error": status_data.get("last_error"),
                    "started_at": format_local(worker.started_at)
                    if worker and worker.started_at
                    else status_data.get("started_at"),
                    "risk_code": status_data.get("risk_code"),
                    "risk_cooldown_until": status_data.get("risk_cooldown_until"),
                    "risk_recovery_required": status_data.get("risk_recovery_required", False),
                    # M2:账号归属(哪个副本持有 + 租约到期时间);单机模式恒为 None。
                    "owner_instance_id": acc.owner_instance_id,
                    "owner_lease_until": format_local(acc.owner_lease_until) or None,
                    "takeover_approved_at": format_local(acc.takeover_approved_at) or None,
                    "takeover_approved_by": acc.takeover_approved_by,
                    "takeover_failure_count": acc.takeover_failure_count,
                    "takeover_last_failure_at": format_local(acc.takeover_last_failure_at) or None,
                }
            )
        return result
