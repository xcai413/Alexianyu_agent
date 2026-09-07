"""AccountPool: manages N AccountWorkers for the runtime daemon.

Workers are built from enabled accounts in the DB and started together (or
individually). Heartbeats are persisted to the worker_status table by each
WsClient, so status is queryable from any process.

P0.2: RuntimeDaemon owns this pool. CLI writes worker_commands and desired_state;
the daemon reconciles this in-memory pool against that persistent control plane.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.events import EventEnvelope
from xianyu_agent.runtime.account_lock import AccountConnectionAlreadyRunningError
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local

EventHandler = Callable[[EventEnvelope], Awaitable[None]]
WorkerFactory = Callable[[str], AccountWorker]
logger = logging.getLogger(__name__)


class AccountPool:
    def __init__(self, *, worker_factory: WorkerFactory | None = None) -> None:
        self._workers: dict[str, AccountWorker] = {}
        self._worker_factory = worker_factory or AccountWorker

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
        worker.start()
        return True

    def start_all(self) -> list[str]:
        started: list[str] = []
        for account_id in list(self._workers):
            try:
                self._workers[account_id].start()
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
        for worker in self._workers.values():
            await worker.stop()

    async def reconcile_desired_accounts(self) -> dict[str, list[str]]:
        """令内存 Worker 集合与持久化期望状态保持一致。"""
        accounts = await domain_accounts.list_desired_running_accounts()
        desired = {account.account_id for account in accounts}
        current = set(self._workers)
        stopped: list[str] = []
        for account_id in sorted(current - desired):
            worker = self._workers.pop(account_id)
            await worker.stop()
            stopped.append(account_id)
        started: list[str] = []
        for account_id in sorted(desired - current):
            worker = self._worker_factory(account_id)
            self._workers[account_id] = worker
            try:
                worker.start()
            except AccountConnectionAlreadyRunningError:
                self._workers.pop(account_id, None)
                logger.warning("account connection lock busy account=%s", account_id)
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
            worker.start()
            return "already_running"
        worker = self._worker_factory(account_id)
        self._workers[account_id] = worker
        try:
            worker.start()
        except Exception:
            self._workers.pop(account_id, None)
            raise
        return "started"

    async def ensure_stopped(self, account_id: str) -> str:
        """确保一个 Worker 已从池中停止并移除。"""
        worker = self._workers.pop(account_id, None)
        if worker is None:
            return "already_stopped"
        await worker.stop()
        return "stopped"

    async def restart_worker(self, account_id: str) -> str:
        """重建一个账号的 Worker,避免复用已停止 client 的瞬时状态。"""
        previous = self._workers.pop(account_id, None)
        if previous is not None:
            await previous.stop()
        worker = self._worker_factory(account_id)
        self._workers[account_id] = worker
        try:
            worker.start()
        except Exception:
            self._workers.pop(account_id, None)
            raise
        return "restarted"

    def restart(self, account_id: str) -> bool:
        worker = self._workers.get(account_id)
        if worker is None:
            return False
        worker.start()
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
            row = by_account.get(acc.account_id) or {}
            result.append(
                {
                    "account_id": acc.account_id,
                    "enabled": acc.enabled,
                    "desired_state": acc.desired_state,
                    "worker_state": worker.state.value if worker else "no_worker",
                    "db_status": row.get("status", "offline"),
                    "reconnect_attempts": row.get("reconnect_attempts", 0),
                    "last_heartbeat_at": row.get("last_heartbeat_at"),
                    "last_error": row.get("last_error"),
                    "started_at": format_local(worker.started_at)
                    if worker and worker.started_at
                    else row.get("started_at"),
                    "risk_code": row.get("risk_code"),
                    "risk_cooldown_until": row.get("risk_cooldown_until"),
                    "risk_recovery_required": row.get("risk_recovery_required", False),
                }
            )
        return result
