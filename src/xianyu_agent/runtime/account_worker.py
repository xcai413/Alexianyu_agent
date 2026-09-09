"""AccountWorker: canonical lifecycle owner for one account runtime.

The worker owns the canonical :class:`WorkerState` lifecycle and composes the
CredentialSupervisor, RecoverySupervisor, and WsClient public contracts. The
WsClient remains transport/protocol-only: its ``CONNECTED`` state is never
interpreted as business readiness. ``ONLINE`` is reached only after the active
connection exposes a canonical ``SubscriptionReady`` marker.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select

from xianyu_agent.application.session.supervisor import CredentialSupervisor
from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, AuditLog, WorkerStatus, get_async_session
from xianyu_agent.domain import events as domain_events
from xianyu_agent.domain.account import risk as worker_risk
from xianyu_agent.domain.message import messages as domain_messages
from xianyu_agent.domain.order import orders as domain_orders
from xianyu_agent.domain.runtime.worker_state import (
    InvalidWorkerStateTransition,
    WorkerState,
    serialize_worker_state,
    transition_worker_state,
    worker_state_from_persistence,
)
from xianyu_agent.infrastructure.session.ws_credentials import (
    LegacyValidationGate,
    LegacyWsCredentialBackend,
)
from xianyu_agent.protocol import event_mapper
from xianyu_agent.protocol.capture import redact_structure
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.event_mapper import ProtocolDomainEvent
from xianyu_agent.protocol.events import (
    ConnectionState,
    ConnectionStateChanged,
    EventEnvelope,
    WsFrame,
)
from xianyu_agent.protocol.parser import parse_frame
from xianyu_agent.protocol.ws_auth import WsAuthError
from xianyu_agent.runtime.account_lock import AccountConnectionLock
from xianyu_agent.runtime.recovery import (
    CredentialRecoveryRoute,
    RecoveryCause,
    RecoveryDecision,
    RecoverySupervisor,
)
from xianyu_agent.services.delivery_service import DeliveryService
from xianyu_agent.services.guardrails import Guardrails, write_guardrail_event
from xianyu_agent.services.reply_engine import ReplyEngine

logger = logging.getLogger(__name__)
RetrySleep = Callable[[float], Awaitable[None]]


class _WorkerConnectionLock(AccountConnectionLock):
    """One lock reservation shared by AccountWorker startup and WsClient lifetime."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._held_by_worker = False

    def acquire(self, *, owner_id: str) -> None:
        if self._held_by_worker:
            return
        super().acquire(owner_id=owner_id)
        self._held_by_worker = True

    def release(self) -> None:
        if not self._held_by_worker:
            return
        try:
            super().release()
        finally:
            self._held_by_worker = False


class _WorkerOwnedWsClient(WsClient):
    """Transport client whose AccountWorker owner exclusively persists lifecycle status."""

    async def _emit_state(self, new_state: ConnectionState, detail: str | None = None) -> None:
        """Propagate canonical lifecycle callback failures instead of suppressing them."""
        self._state = new_state
        event = ConnectionStateChanged(
            event_id=uuid.uuid4().hex,
            account_id=self.account_id,
            state=new_state,
            detail=detail,
            occurred_at=datetime.now(UTC),
        )
        await self._update_worker_status(state=new_state, detail=detail)
        if self.on_state is None:
            return
        try:
            await self.on_state(event)
        except Exception:
            # Standalone WsClient keeps its compatibility suppression. An
            # AccountWorker-owned transport must stop and surface canonical
            # persistence/lifecycle failures instead of silently reconnecting.
            self._stop.set()
            self._release_account_lock()
            raise

    async def _update_worker_status(
        self,
        *,
        state: ConnectionState,
        detail: str | None,
    ) -> None:
        """Preserve transport diagnostics without overwriting canonical WorkerState."""
        try:
            async with get_async_session() as session:
                account = (
                    await session.execute(
                        select(Account).where(Account.account_id == self.account_id).limit(1)
                    )
                ).scalar_one_or_none()
                if account is None:
                    return
                row = (
                    await session.execute(
                        select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
                    )
                ).scalar_one_or_none()
                # AccountWorker persists STARTING before transport start, so a missing
                # row means there is no canonical lifecycle row to annotate yet.
                if row is None:
                    return
                if state is ConnectionState.DISCONNECTED and row.risk_recovery_required:
                    await session.commit()
                    return
                if state is ConnectionState.CONNECTED:
                    row.reconnect_attempts = self._reconnect_attempts
                    row.last_heartbeat_at = datetime.now(UTC)
                    row.started_at = row.started_at or datetime.now(UTC)
                    row.last_error = None
                if detail and state in {ConnectionState.ERROR, ConnectionState.DISCONNECTED}:
                    row.last_error = detail
                await session.commit()
        except Exception as exc:
            logger.warning("worker transport diagnostics update failed: %s", exc)


class AccountWorker:
    """Own one account's canonical lifecycle and business-event wiring."""

    def __init__(
        self,
        account_id: str,
        *,
        client: WsClient | None = None,
        persist_events: bool = True,
        reply_engine: ReplyEngine | None = None,
        delivery_service: DeliveryService | None = None,
        guardrails: Guardrails | None = None,
        automation_mode: str | None = None,
        credential_supervisor: CredentialSupervisor[Any] | None = None,
        recovery_supervisor: RecoverySupervisor[Any] | None = None,
        retry_sleep: RetrySleep = asyncio.sleep,
        readiness_poll_s: float = 0.01,
    ) -> None:
        self.account_id = account_id
        self.started_at: datetime | None = None
        self._reply_engine = reply_engine
        self._delivery_service = delivery_service
        self._guardrails = guardrails
        settings = get_settings()
        self._automation_mode = automation_mode or settings.automation_mode
        self._connection_lock: _WorkerConnectionLock | None = None
        if client is None:
            self._connection_lock = _WorkerConnectionLock(settings.account_lock_path(account_id))
            self._client = _WorkerOwnedWsClient(
                account_id,
                on_event=self._on_event if persist_events else None,
                on_auth_failure=self._on_auth_failure,
                account_lock=self._connection_lock,
            )
        else:
            self._client = client
        self._previous_state_handler = getattr(self._client, "on_state", None)
        self._client.on_state = self._on_client_state
        self._client.on_auth_failure = self._on_auth_failure

        self._credentials = credential_supervisor or self._build_default_credentials()
        self._recovery = recovery_supervisor or RecoverySupervisor(self._credentials)
        self._retry_sleep = retry_sleep
        self._readiness_poll_s = max(0.001, readiness_poll_s)
        self._worker_state = WorkerState.DISABLED
        self._state_history: list[WorkerState] = [WorkerState.DISABLED]
        self._state_transition_lock = asyncio.Lock()
        self._restored_state: WorkerState | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._readiness_task: asyncio.Task[None] | None = None
        self._injected_queue: asyncio.Queue[WsFrame] | None = None
        self._injected_task: asyncio.Task[None] | None = None
        self._stop_requested = False

        if self._guardrails is None and self._automation_mode == "active":
            self._guardrails = Guardrails()
        if self._reply_engine is None and persist_events and self._automation_mode == "active":
            self._reply_engine = ReplyEngine(sender=self._send_reply)
        if (
            self._delivery_service is None
            and persist_events
            and self._automation_mode == "active"
        ):
            self._delivery_service = DeliveryService(
                sender=self._send_reply,
                guardrails=self._guardrails,
            )

    def _build_default_credentials(self) -> CredentialSupervisor[Any] | None:
        """Adapt the WsClient credential primitives without reimplementing them."""
        signer = getattr(self._client, "signer", None)
        provider = getattr(self._client, "token_provider", None)
        if signer is None or provider is None:
            return None
        backend = LegacyWsCredentialBackend(signer=signer, provider=provider)
        return CredentialSupervisor(backend, LegacyValidationGate())

    async def _on_auth_failure(self, error: WsAuthError) -> bool:
        """Recover auth failures or fail closed on validation/terminal errors."""
        if worker_risk.is_user_validate_error(error):
            await worker_risk.open_user_validate(self.account_id)
            decision = self._recovery.decide(RecoveryCause.NEEDS_VALIDATION)
            await self._apply_recovery_decision(decision)
            return True

        await self._prepare_credential_recovery(WorkerState.REFRESHING_CREDENTIAL)
        if self._credentials is None:
            await self._set_worker_state(WorkerState.ERROR, detail="credential supervisor missing")
            return True
        recovered = await self._recover_credentials(CredentialRecoveryRoute.REFRESH)
        # WsClient owns transport retry. False lets it reconnect using refreshed material.
        return not recovered

    async def _on_client_state(self, event: ConnectionStateChanged) -> None:
        """Project transport observations into the canonical worker lifecycle."""
        try:
            if event.state is ConnectionState.CONNECTING:
                await self._advance_to_connecting()
            elif event.state is ConnectionState.CONNECTED:
                await self._advance_transport_connected()
            elif (
                event.state in {ConnectionState.RECONNECTING, ConnectionState.ERROR}
                or (
                    event.state is ConnectionState.DISCONNECTED
                    and self._worker_state
                    not in {
                        WorkerState.DISABLED,
                        WorkerState.STOPPING,
                        WorkerState.NEEDS_VALIDATION,
                        WorkerState.ERROR,
                    }
                )
            ):
                await self._handle_transport_failure(event.detail)
        except InvalidWorkerStateTransition as exc:
            logger.exception("worker lifecycle transition rejected account=%s", self.account_id)
            await self._persist_current_worker_state(detail=str(exc))
        finally:
            if self._previous_state_handler is not None:
                with suppress(Exception):
                    await self._previous_state_handler(event)

    async def _handle_transport_failure(self, detail: str | None) -> None:
        if self._worker_state in {
            WorkerState.STOPPING,
            WorkerState.DISABLED,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.ERROR,
        }:
            return
        decision = self._recovery.decide(RecoveryCause.TRANSPORT_FAILURE, code=detail)
        await self._apply_recovery_decision(decision)

    async def _advance_to_connecting(self) -> None:
        if self._worker_state in {
            WorkerState.ONLINE,
            WorkerState.SYNCING,
            WorkerState.REGISTERING,
        }:
            await self._set_worker_state(WorkerState.RECONNECTING)
        if self._worker_state in {WorkerState.RECONNECTING, WorkerState.CHECKING_SESSION}:
            await self._set_worker_state(WorkerState.CONNECTING)
        elif self._worker_state is WorkerState.REFRESHING_CREDENTIAL:
            await self._set_worker_state(WorkerState.CHECKING_SESSION)
            await self._set_worker_state(WorkerState.CONNECTING)

    async def _advance_transport_connected(self) -> None:
        """Transport CONNECTED proves registration path only; never ONLINE directly."""
        await self._cancel_readiness_monitor()
        if self._worker_state in {WorkerState.RECONNECTING, WorkerState.CHECKING_SESSION}:
            await self._set_worker_state(WorkerState.CONNECTING)
        if self._worker_state is WorkerState.CONNECTING:
            await self._set_worker_state(WorkerState.REGISTERING)
        if self._worker_state is WorkerState.REGISTERING:
            await self._set_worker_state(WorkerState.SYNCING)
        if self._worker_state is not WorkerState.SYNCING:
            return
        if getattr(self._client, "subscription_ready", None) is not None:
            await self._set_worker_state(WorkerState.ONLINE)
            return
        self._readiness_task = asyncio.create_task(
            self._watch_subscription_ready(),
            name=f"worker-readiness-{self.account_id}",
        )

    async def _watch_subscription_ready(self) -> None:
        """Promote SYNCING to ONLINE only for the active connection's ready marker."""
        try:
            while (
                not self._stop_requested
                and self._worker_state is WorkerState.SYNCING
                and getattr(self._client, "state", None) is ConnectionState.CONNECTED
            ):
                if getattr(self._client, "subscription_ready", None) is not None:
                    await self._set_worker_state(WorkerState.ONLINE)
                    return
                await asyncio.sleep(self._readiness_poll_s)
        except asyncio.CancelledError:
            raise
        except InvalidWorkerStateTransition:
            logger.exception("worker readiness transition rejected account=%s", self.account_id)

    async def _on_event(self, event: EventEnvelope) -> None:
        """Map one protocol DTO, then route only canonical events downstream."""
        domain_event = event_mapper.to_domain_event(cast(ProtocolDomainEvent, event))
        raw_payload = redact_structure(event.raw) if event.raw else None

        if isinstance(domain_event, domain_events.MessageReceived):
            message_id = await domain_messages.upsert_inbound(
                domain_event,
                raw_payload=raw_payload,
            )
            if self._reply_engine is not None and self._guardrails is not None:
                decision = await self._guardrails.check_message(
                    domain_event.account_id,
                    domain_event.content,
                )
                if decision.allowed:
                    await self._reply_engine.handle(domain_event, message_id=message_id)
                else:
                    await write_guardrail_event(
                        domain_event.account_id,
                        rule="message_gate",
                        detail=decision.reason or "",
                    )
        elif isinstance(domain_event, domain_events.MessageSent):
            await domain_messages.record_outbound(domain_event)
        elif isinstance(
            domain_event,
            (domain_events.OrderCreated, domain_events.OrderPaid, domain_events.OrderDelivered),
        ):
            await domain_orders.upsert_from_event(domain_event, raw_payload=raw_payload)
            if isinstance(domain_event, domain_events.OrderPaid) and self._delivery_service is not None:
                await self._delivery_service.deliver(domain_event)
        elif isinstance(domain_event, domain_events.SystemNotice):
            await self._record_system_notice(domain_event)

    async def _record_system_notice(self, event: domain_events.SystemNotice) -> None:
        """系统提示单独入审计日志,永不进入买家消息自动化。"""
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == event.account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return
            session.add(
                AuditLog(
                    actor="system",
                    action="protocol.system_notice",
                    target=event.account_id,
                    params={
                        "notice_type": event.notice_type,
                        "content": event.content[:500],
                    },
                    result="observed",
                )
            )
            await session.commit()

    async def _send_reply(self, _account_id: str, _chat_id: str, text: str) -> bool:
        return await self._client.send_text(text)

    def start(self) -> asyncio.Task[None] | None:
        """Schedule startup and return the task that owns its durable outcome."""
        if self._worker_state is WorkerState.NEEDS_VALIDATION:
            return None
        if self._startup_task is not None and not self._startup_task.done():
            return self._startup_task
        if self._worker_state not in {WorkerState.DISABLED, WorkerState.ERROR}:
            return None

        if self._connection_lock is not None:
            self._connection_lock.acquire(
                owner_id=f"worker:{self.account_id}:{uuid.uuid4().hex}"
            )
        try:
            self._stop_requested = False
            self._startup_task = asyncio.create_task(
                self._run_startup(),
                name=f"worker-startup-{self.account_id}",
            )
            return self._startup_task
        except Exception:
            self.started_at = None
            if self._connection_lock is not None:
                self._connection_lock.release()
            raise

    async def _run_startup(self) -> None:
        transport_started = False
        starting_committed = False
        try:
            restored = await self._load_persisted_worker_state()
            self._restored_state = restored
            await self._set_worker_state(WorkerState.STARTING)
            starting_committed = True
            self.started_at = datetime.now(UTC)

            if restored is WorkerState.NEEDS_VALIDATION:
                await self._set_worker_state(WorkerState.NEEDS_VALIDATION)
                return

            await self._set_worker_state(WorkerState.CHECKING_SESSION)
            if self._credentials is not None:
                ready = await self._recover_credentials(CredentialRecoveryRoute.ENSURE)
                if not ready:
                    return
            else:
                await self._set_worker_state(WorkerState.CONNECTING)

            if self._stop_requested:
                return
            self._client.start()
            transport_started = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("worker startup failed account=%s", self.account_id)
            if not starting_committed:
                self.started_at = None
                raise
            if self._worker_state not in {
                WorkerState.STOPPING,
                WorkerState.DISABLED,
                WorkerState.NEEDS_VALIDATION,
            }:
                with suppress(InvalidWorkerStateTransition):
                    await self._set_worker_state(WorkerState.ERROR, detail=str(exc))
        finally:
            if not transport_started and self._connection_lock is not None:
                self._connection_lock.release()

    async def _recover_credentials(self, route: CredentialRecoveryRoute) -> bool:
        """Execute a supervisor route until success or a fail-closed terminal decision."""
        attempt = 1
        while not self._stop_requested:
            outcome = await self._recovery.recover_credential(
                self.account_id,
                route=route,
                attempt=attempt,
            )
            decision = outcome.decision
            result = outcome.credential_result

            if decision.retry:
                await self._apply_recovery_decision(decision)
                await self._retry_sleep(decision.retry_delay_s or 0.0)
                attempt += 1
                continue
            if decision.worker_state is WorkerState.NEEDS_VALIDATION:
                await self._apply_recovery_decision(decision)
                return False
            if decision.worker_state is WorkerState.ERROR:
                await self._apply_recovery_decision(decision)
                return False
            if (
                result is not None
                and result.refreshed
                and self._worker_state is WorkerState.CHECKING_SESSION
            ):
                await self._set_worker_state(WorkerState.REFRESHING_CREDENTIAL)
                await self._set_worker_state(WorkerState.CHECKING_SESSION)
            await self._apply_recovery_decision(decision)
            return True
        return False

    async def _prepare_credential_recovery(self, target: WorkerState) -> None:
        if self._worker_state in {
            WorkerState.CONNECTING,
            WorkerState.REGISTERING,
            WorkerState.SYNCING,
            WorkerState.ONLINE,
        }:
            await self._set_worker_state(WorkerState.RECONNECTING)
        if target is WorkerState.REFRESHING_CREDENTIAL and self._worker_state in {
            WorkerState.RECONNECTING,
            WorkerState.CHECKING_SESSION,
        }:
            await self._set_worker_state(WorkerState.REFRESHING_CREDENTIAL)

    async def _apply_recovery_decision(self, decision: RecoveryDecision) -> None:
        """Converge to a RecoverySupervisor target without skipping the state graph."""
        target = decision.worker_state
        if target is self._worker_state:
            await self._set_worker_state(target, detail=decision.code)
            return
        if target is WorkerState.RECONNECTING:
            await self._set_worker_state(target, detail=decision.code)
            await self._cancel_readiness_monitor()
            return
        if target is WorkerState.CHECKING_SESSION:
            if self._worker_state in {
                WorkerState.CONNECTING,
                WorkerState.REGISTERING,
                WorkerState.SYNCING,
                WorkerState.ONLINE,
            }:
                await self._set_worker_state(WorkerState.RECONNECTING)
            if self._worker_state in {
                WorkerState.REFRESHING_CREDENTIAL,
                WorkerState.RECONNECTING,
            }:
                await self._set_worker_state(WorkerState.CHECKING_SESSION, detail=decision.code)
            return
        if target is WorkerState.REFRESHING_CREDENTIAL:
            await self._prepare_credential_recovery(target)
            return
        if target is WorkerState.CONNECTING:
            if self._worker_state is WorkerState.REFRESHING_CREDENTIAL:
                await self._set_worker_state(WorkerState.CHECKING_SESSION)
            if self._worker_state is WorkerState.RECONNECTING:
                await self._set_worker_state(WorkerState.CHECKING_SESSION)
            await self._set_worker_state(WorkerState.CONNECTING, detail=decision.code)
            return
        await self._set_worker_state(target, detail=decision.code)

    async def recover_validation(self) -> bool:
        """Explicit operator-triggered validation recovery; never called automatically."""
        if self._worker_state is not WorkerState.NEEDS_VALIDATION or self._credentials is None:
            return False
        outcome = await self._recovery.recover_credential(
            self.account_id,
            route=CredentialRecoveryRoute.VALIDATION_REFRESH,
        )
        decision = outcome.decision
        if decision.retry:
            await self._set_worker_state(WorkerState.NEEDS_VALIDATION, detail=decision.code)
            return False
        if decision.worker_state is not WorkerState.CHECKING_SESSION:
            await self._apply_recovery_decision(decision)
            return False

        if self._connection_lock is not None:
            self._connection_lock.acquire(
                owner_id=f"worker-validation:{self.account_id}:{uuid.uuid4().hex}"
            )
        try:
            await self._set_worker_state(WorkerState.CHECKING_SESSION, detail=decision.code)
            await self._set_worker_state(WorkerState.CONNECTING)
            self._stop_requested = False
            self.started_at = self.started_at or datetime.now(UTC)
            self._client.start()
        except Exception:
            if self._connection_lock is not None:
                self._connection_lock.release()
            raise
        return True

    def inject_frame(self, frame: Any) -> None:
        """Replay synthetic frames serially without depending on live credential startup."""
        if self._injected_queue is None:
            self._injected_queue = asyncio.Queue()
        self._injected_queue.put_nowait(cast(WsFrame, frame))
        task = self._injected_task
        if task is None or task.done():
            self._injected_task = asyncio.create_task(
                self._pump_injected_frames(),
                name=f"worker-inject-{self.account_id}",
            )

    async def _pump_injected_frames(self) -> None:
        current = asyncio.current_task()
        try:
            while True:
                queue = self._injected_queue
                if queue is None:
                    return
                try:
                    frame = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._dispatch_injected_frame(frame)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("synthetic frame dispatch failed account=%s", self.account_id)
                finally:
                    queue.task_done()
        finally:
            if self._injected_task is current:
                self._injected_task = None

    async def _dispatch_injected_frame(self, frame: WsFrame) -> None:
        receiver = getattr(self._client, "on_event", None)
        if receiver is None:
            return
        for event in parse_frame(frame, self.account_id):
            await receiver(event)

    async def drain_injected_frames(self) -> None:
        """Wait until all synthetic frames already queued for this worker are consumed."""
        await self._drain_injected_frames()

    async def send_text(self, text: str) -> bool:
        return await self._client.send_text(text)

    async def stop(self) -> None:
        if self._worker_state is WorkerState.DISABLED:
            await self._drain_injected_frames()
            if self._connection_lock is not None:
                self._connection_lock.release()
            self.started_at = None
            return
        self._stop_requested = True
        if self._worker_state is not WorkerState.STOPPING:
            await self._set_worker_state(WorkerState.STOPPING)
        await self._cancel_readiness_monitor()
        startup = self._startup_task
        if startup is not None and not startup.done() and startup is not asyncio.current_task():
            startup.cancel()
            with suppress(asyncio.CancelledError):
                await startup
        await self._drain_injected_frames()
        await self._client.stop()
        if self._connection_lock is not None:
            self._connection_lock.release()
        await self._set_worker_state(WorkerState.DISABLED)
        self.started_at = None

    async def _drain_injected_frames(self) -> None:
        queue = self._injected_queue
        if queue is not None:
            await queue.join()
        task = self._injected_task
        if task is not None and task is not asyncio.current_task():
            await task

    async def _cancel_readiness_monitor(self) -> None:
        task = self._readiness_task
        self._readiness_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _set_worker_state(
        self,
        target: WorkerState,
        *,
        detail: str | None = None,
    ) -> None:
        async with self._state_transition_lock:
            next_state = transition_worker_state(self._worker_state, target)
            await self._persist_worker_state(next_state, detail=detail)
            if next_state is not self._worker_state:
                self._worker_state = next_state
                self._state_history.append(next_state)

    async def _persist_current_worker_state(self, *, detail: str | None = None) -> None:
        async with self._state_transition_lock:
            await self._persist_worker_state(self._worker_state, detail=detail)

    async def _load_persisted_worker_state(self) -> WorkerState | None:
        """Normalize canonical or legacy status, using durable validation as authority."""
        try:
            async with get_async_session() as session:
                account = (
                    await session.execute(
                        select(Account).where(Account.account_id == self.account_id).limit(1)
                    )
                ).scalar_one_or_none()
                if account is None:
                    return None
                row = (
                    await session.execute(
                        select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                return worker_state_from_persistence(
                    str(row.status),
                    risk_recovery_required=bool(row.risk_recovery_required),
                )
        except Exception as exc:
            logger.warning("worker persisted state read failed account=%s: %s", self.account_id, exc)
            return None

    async def _persist_worker_state(
        self,
        state: WorkerState,
        *,
        detail: str | None = None,
    ) -> None:
        """Persist the canonical WorkerState while retaining legacy read compatibility."""
        try:
            async with get_async_session() as session:
                account = (
                    await session.execute(
                        select(Account).where(Account.account_id == self.account_id).limit(1)
                    )
                ).scalar_one_or_none()
                if account is None:
                    return
                row = (
                    await session.execute(
                        select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
                    )
                ).scalar_one_or_none()
                persisted = serialize_worker_state(state)
                if row is None:
                    row = WorkerStatus(account_id=account.id, status=persisted)
                    session.add(row)
                else:
                    row.status = persisted
                if state is WorkerState.ONLINE:
                    row.last_error = None
                    row.last_heartbeat_at = datetime.now(UTC)
                    row.started_at = row.started_at or self.started_at or datetime.now(UTC)
                elif detail and state in {
                    WorkerState.ERROR,
                    WorkerState.NEEDS_VALIDATION,
                    WorkerState.RECONNECTING,
                }:
                    row.last_error = detail
                await session.commit()
        except Exception as exc:
            logger.warning("worker state persistence failed account=%s: %s", self.account_id, exc)
            raise

    @property
    def state(self) -> WorkerState:
        """Canonical AccountWorker runtime state."""
        return self._worker_state

    @property
    def connection_state(self) -> ConnectionState:
        """Legacy transport state for compatibility diagnostics."""
        return self._client.state

    @property
    def restored_state(self) -> WorkerState | None:
        """Normalized persisted state observed during the latest startup."""
        return self._restored_state

    @property
    def state_history(self) -> tuple[WorkerState, ...]:
        """In-process lifecycle history used for diagnostics and regression tests."""
        return tuple(self._state_history)

    @property
    def is_running(self) -> bool:
        return self.started_at is not None and self._worker_state not in {
            WorkerState.DISABLED,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.ERROR,
        }
