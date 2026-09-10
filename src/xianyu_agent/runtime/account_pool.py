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

from xianyu_agent.domain.account import state as domain_accounts
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.protocol.events import EventEnvelope
from xianyu_agent.runtime.account_lock import AccountConnectionAlreadyRunningError
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local

EventHandler = Callable[[EventEnvelope], Awaitable[None]]
WorkerFactory = Callable[[str], AccountWorker]
logger = logging.getLogger(__name__)


class AccountPool:
    def __init__(self, *, worker_factory: WorkerFactory | None = None) -> None:
        self._workers: dict[str, AccountWorker] = {}
        self._worker_factory = worker_factory or AccountWorker
        self._startup_tasks: dict[str, asyncio.Task[None]] = {}
        self._transport_tasks: dict[str, asyncio.Task[None]] = {}
        self._lifecycle_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._retirement_barriers: dict[str, asyncio.Future[None]] = {}

    @classmethod
    async def from_enabled_accounts(
        cls,
        *,
        on_event: EventHandler | None = None,
    ) -> AccountPool:
        """Build a pool with one worker per enabled account."""

        def _build_worker(account_id: str) -> AccountWorker:
            worker = AccountWorker(account_id, persist_events=on_event is None)
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
    ) -> AccountPool:
        """Build a pool from enabled accounts whose desired_state is running."""

        def _build_worker(account_id: str) -> AccountWorker:
            worker = AccountWorker(account_id, persist_events=on_event is None)
            if on_event is not None:
                worker._client.on_event = on_event
            return worker

        pool = cls(worker_factory=_build_worker)
        accounts = await domain_accounts.list_desired_running_accounts()
        for acc in accounts:
            pool._workers[acc.account_id] = pool._worker_factory(acc.account_id)
        return pool

    def _observe_startup(
        self,
        account_id: str,
        worker: AccountWorker,
        task: asyncio.Task[None] | None,
    ) -> None:
        """Own a scheduled startup outcome for synchronous pool entry points."""
        if task is None:
            self._observe_transport_task(account_id, worker)
            return
        self._startup_tasks[account_id] = task

        def _completed(completed: asyncio.Task[None]) -> None:
            if self._startup_tasks.get(account_id) is completed:
                self._startup_tasks.pop(account_id, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is None and worker.state is not WorkerState.ERROR:
                self._observe_transport_task(account_id, worker)
                return
            if self._workers.get(account_id) is worker:
                self._workers.pop(account_id, None)
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
            if isinstance(lifecycle_error, Exception):
                cleanup = asyncio.create_task(
                    self._retire_failed_transport(account_id, worker, lifecycle_error),
                    name=f"worker-lifecycle-failure-{account_id}",
                )
            elif isinstance(error, Exception):
                cleanup = asyncio.create_task(
                    self._retire_failed_transport(account_id, worker, error),
                    name=f"worker-lifecycle-failure-{account_id}",
                )
            elif not worker._stop_requested and worker.state is WorkerState.ERROR:
                cleanup = asyncio.create_task(
                    self._retire_terminal_transport(account_id, worker),
                    name=f"worker-terminal-transport-{account_id}",
                )
            else:
                return
            self._lifecycle_cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._lifecycle_cleanup_tasks.discard)

        task.add_done_callback(_completed)

    def _claim_worker_retirement(
        self,
        account_id: str,
        worker: AccountWorker,
    ) -> asyncio.Future[None] | None:
        """Claim one account generation for exclusive physical cleanup."""
        if account_id in self._retirement_barriers:
            return None
        if self._workers.get(account_id) is not worker:
            return None
        barrier = asyncio.get_running_loop().create_future()
        self._retirement_barriers[account_id] = barrier
        self._workers.pop(account_id, None)
        return barrier

    def _finish_worker_retirement(
        self,
        account_id: str,
        barrier: asyncio.Future[None],
    ) -> None:
        """Release a retirement barrier only after old physical cleanup completed."""
        if self._retirement_barriers.get(account_id) is barrier:
            self._retirement_barriers.pop(account_id, None)
        if not barrier.done():
            barrier.set_result(None)

    async def _await_worker_retirement(self, account_id: str) -> None:
        """Wait until no older worker generation for this account is retiring."""
        while True:
            barrier = self._retirement_barriers.get(account_id)
            if barrier is None:
                return
            await asyncio.shield(barrier)

    async def _drain_observed_transport_tasks(self) -> None:
        """Drain observed transports so their completion callbacks can register cleanup."""
        while self._transport_tasks:
            tasks = tuple(self._transport_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drain_lifecycle_cleanup_tasks(self) -> None:
        """Drain pool-owned lifecycle cleanup tasks, including tasks created while draining."""
        while self._lifecycle_cleanup_tasks:
            tasks = tuple(self._lifecycle_cleanup_tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error(
                        "account worker lifecycle cleanup task failed",
                        exc_info=(type(result), result, result.__traceback__),
                    )

    async def _drain_retirement_barriers(self) -> None:
        """Wait for retirement owners not represented by the worker map."""
        while self._retirement_barriers:
            barriers = tuple(self._retirement_barriers.values())
            await asyncio.gather(*(asyncio.shield(barrier) for barrier in barriers))

    async def _retire_failed_transport(
        self,
        account_id: str,
        worker: AccountWorker,
        error: Exception,
    ) -> None:
        """Route transport-task failure through exclusive AccountPool cleanup ownership."""
        barrier = self._claim_worker_retirement(account_id, worker)
        if barrier is None:
            return
        try:
            await worker._fail_closed_transport_after_lifecycle_error(error)
        except Exception:
            logger.exception(
                "account worker lifecycle fail-closed cleanup failed account=%s",
                account_id,
            )
        finally:
            self._finish_worker_retirement(account_id, barrier)
            logger.error(
                "account worker transport lifecycle failed account=%s error=%s",
                account_id,
                error,
            )

    async def _retire_terminal_transport(
        self,
        account_id: str,
        worker: AccountWorker,
    ) -> None:
        """Retire a normally-ended transport whose owner reached terminal ERROR."""
        barrier = self._claim_worker_retirement(account_id, worker)
        if barrier is None:
            return
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "account worker terminal transport cleanup failed account=%s",
                account_id,
            )
        finally:
            self._finish_worker_retirement(account_id, barrier)
            logger.error(
                "account worker transport ended in terminal state account=%s state=%s",
                account_id,
                worker.state.value,
            )

    def _start_observed(
        self,
        account_id: str,
        worker: AccountWorker,
    ) -> asyncio.Task[None] | None:
        task = worker.start()
        self._observe_startup(account_id, worker, task)
        return task

    @property
    def account_ids(self) -> list[str]:
        return list(self._workers)

    def has(self, account_id: str) -> bool:
        return account_id in self._workers

    def get(self, account_id: str) -> AccountWorker | None:
        return self._workers.get(account_id)

    def start(self, account_id: str) -> bool:
        """Start one worker. Returns False if account is unknown."""
        worker = self._workers.get(account_id)
        if worker is None:
            return False
        self._start_observed(account_id, worker)
        return True

    def start_all(self) -> list[str]:
        started: list[str] = []
        for account_id in list(self._workers):
            worker = self._workers[account_id]
            try:
                self._start_observed(account_id, worker)
            except AccountConnectionAlreadyRunningError:
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
        claimed: list[tuple[str, AccountWorker, asyncio.Future[None]]] = []
        for account_id, worker in list(self._workers.items()):
            barrier = self._claim_worker_retirement(account_id, worker)
            if barrier is not None:
                claimed.append((account_id, worker, barrier))

        errors: list[Exception] = []
        for account_id, worker, barrier in claimed:
            try:
                await worker.stop()
            except Exception as exc:
                errors.append(exc)
                logger.exception("account worker stop failed account=%s", account_id)
            finally:
                self._finish_worker_retirement(account_id, barrier)

        await self._drain_observed_transport_tasks()
        await self._drain_lifecycle_cleanup_tasks()
        await self._drain_retirement_barriers()

        if errors:
            raise errors[0]

    async def reconcile_desired_accounts(self) -> dict[str, list[str]]:
        """令内存 Worker 集合与持久化期望状态保持一致。"""
        accounts = await domain_accounts.list_desired_running_accounts()
        desired = {account.account_id for account in accounts}
        current = set(self._workers)
        stopped: list[str] = []
        for account_id in sorted(current - desired):
            worker = self._workers.get(account_id)
            if worker is None:
                continue
            barrier = self._claim_worker_retirement(account_id, worker)
            if barrier is None:
                continue
            try:
                await worker.stop()
            finally:
                self._finish_worker_retirement(account_id, barrier)
            stopped.append(account_id)
        started: list[str] = []
        for account_id in sorted(desired - current):
            if account_id in self._retirement_barriers:
                continue
            worker = self._worker_factory(account_id)
            self._workers[account_id] = worker
            try:
                self._start_observed(account_id, worker)
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

    def ensure_started(self, account_id: str) -> str:
        """确保一个 Worker 在池中运行。"""
        worker = self._workers.get(account_id)
        if worker is not None:
            self._start_observed(account_id, worker)
            return "already_running"
        if account_id in self._retirement_barriers:
            return "retiring"
        worker = self._worker_factory(account_id)
        self._workers[account_id] = worker
        try:
            self._start_observed(account_id, worker)
        except Exception:
            self._workers.pop(account_id, None)
            raise
        return "started"

    async def ensure_stopped(self, account_id: str) -> str:
        """确保一个 Worker 已从池中停止并移除。"""
        existing_barrier = self._retirement_barriers.get(account_id)
        if existing_barrier is not None:
            await asyncio.shield(existing_barrier)
            return "already_stopped"
        worker = self._workers.get(account_id)
        if worker is None:
            return "already_stopped"
        barrier = self._claim_worker_retirement(account_id, worker)
        if barrier is None:
            await self._await_worker_retirement(account_id)
            return "already_stopped"
        try:
            await worker.stop()
        finally:
            self._finish_worker_retirement(account_id, barrier)
        return "stopped"

    async def restart_worker(self, account_id: str) -> str:
        """重建一个账号的 Worker,避免复用已停止 client 的瞬时状态。"""
        await self._await_worker_retirement(account_id)
        previous = self._workers.get(account_id)
        if previous is not None:
            barrier = self._claim_worker_retirement(account_id, previous)
            if barrier is None:
                await self._await_worker_retirement(account_id)
            else:
                try:
                    await previous.stop()
                finally:
                    self._finish_worker_retirement(account_id, barrier)
        await self._await_worker_retirement(account_id)
        worker = self._worker_factory(account_id)
        self._workers[account_id] = worker
        try:
            self._start_observed(account_id, worker)
        except Exception:
            if self._workers.get(account_id) is worker:
                self._workers.pop(account_id, None)
            raise
        return "restarted"

    def restart(self, account_id: str) -> bool:
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
                }
            )
        return result
