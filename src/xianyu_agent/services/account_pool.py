"""AccountPool: manages N AccountWorkers.

Workers are built from enabled accounts in the DB and started together (or
individually). Heartbeats are persisted to the worker_status table by each
WsClient, so status is queryable from any process.

Cross-process note: `pool start-all` / `pool start` run in the foreground of
one CLI process. `pool stop` cannot reach workers in another process; it marks
the worker_status row offline (see domain.accounts.mark_worker_offline).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.events import EventEnvelope
from xianyu_agent.services.account_worker import AccountWorker

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class AccountPool:
    def __init__(self) -> None:
        self._workers: dict[str, AccountWorker] = {}

    @classmethod
    async def from_enabled_accounts(
        cls,
        *,
        on_event: EventHandler | None = None,
    ) -> AccountPool:
        """Build a pool with one worker per enabled account."""
        pool = cls()
        accounts = await domain_accounts.list_accounts(only_enabled=True)
        for acc in accounts:
            worker = AccountWorker(acc.account_id, persist_events=on_event is None)
            if on_event is not None:
                worker._client.on_event = on_event
            pool._workers[acc.account_id] = worker
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
        for account_id in self._workers:
            self._workers[account_id].start()
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
                "last_heartbeat_at": row.last_heartbeat_at.isoformat(timespec="seconds")
                if row.last_heartbeat_at
                else None,
                "last_error": row.last_error,
                "started_at": row.started_at.isoformat(timespec="seconds")
                if row.started_at
                else None,
            }
        result: list[dict] = []
        for acc in accounts:
            worker = self._workers.get(acc.account_id)
            row = by_account.get(acc.account_id) or {}
            result.append(
                {
                    "account_id": acc.account_id,
                    "enabled": acc.enabled,
                    "worker_state": worker.state.value if worker else "no_worker",
                    "db_status": row.get("status", "offline"),
                    "reconnect_attempts": row.get("reconnect_attempts", 0),
                    "last_heartbeat_at": row.get("last_heartbeat_at"),
                    "last_error": row.get("last_error"),
                    "started_at": worker.started_at.isoformat(timespec="seconds")
                    if worker and worker.started_at
                    else row.get("started_at"),
                }
            )
        return result
