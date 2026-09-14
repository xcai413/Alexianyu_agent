"""Canonical single-account WebSocket client orchestration.

This module owns one receive loop per WebSocket connection and composes the
transport/protocol primitives under :mod:`xianyu_agent.protocol.ws`. Runtime
WorkerState/Recovery/Credential orchestration remains outside this boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, get_async_session
from xianyu_agent.db.models import WorkerStatus as DbWorkerStatus
from xianyu_agent.protocol.events import (
    ConnectionState,
    ConnectionStateChanged,
    ErrorOccurred,
    EventEnvelope,
    WsFrame,
)
from xianyu_agent.protocol.parser import parse_frame
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.protocol.ws import (
    ack as ws_ack,
    connector as ws_connector,
    decoder as ws_decoder,
    heartbeat as ws_heartbeat,
    history as ws_history,
    request_router as ws_request_router,
    sender as ws_sender,
    sync as ws_sync,
)
from xianyu_agent.protocol.ws_auth import (
    WsAuthError,
    WsTokenProvider,
    build_registration_frame,
    build_sync_frame,
)
from xianyu_agent.services.account_lock import AccountConnectionLock

logger = logging.getLogger(__name__)
EventHandler = Callable[[EventEnvelope], Awaitable[None]]
FrameHandler = Callable[[WsFrame], Awaitable[None]]
StateHandler = Callable[[ConnectionStateChanged], Awaitable[None]]
ErrorHandler = Callable[[ErrorOccurred], Awaitable[None]]
AuthFailureHandler = Callable[[WsAuthError], Awaitable[bool]]
DispatchCallback = Callable[[], Awaitable[None]]
DeferredBusinessEvents = tuple[EventEnvelope, ...]
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
MAX_DEFERRED_SYNC_FRAMES = 64
MAX_CALLBACK_QUEUE_SIZE = 512

# Compatibility aliases retained for callers/tests that patch the legacy hooks.
_generate_mid = ws_heartbeat.generate_mid
_default_heartbeat = ws_heartbeat.default_heartbeat


@dataclass
class ClientConfig:
    ws_url: str
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    heartbeat_builder: Callable[[], str] = _default_heartbeat
    min_backoff_s: float = MIN_BACKOFF_S
    max_backoff_s: float = MAX_BACKOFF_S
    registration_delay_s: float = 1.0
    auth_retry_delay_s: float = 300.0
    stop_timeout_s: float = 10.0
    registration_timeout_s: float = 5.0

    @classmethod
    def from_settings(cls) -> ClientConfig:
        settings = get_settings()
        return cls(ws_url=settings.ws_url)


class WsClient:
    """Single-account WebSocket client with one receive owner per connection."""

    def __init__(
        self,
        account_id: str,
        *,
        on_event: EventHandler | None = None,
        on_state: StateHandler | None = None,
        on_error: ErrorHandler | None = None,
        on_auth_failure: AuthFailureHandler | None = None,
        on_frame: FrameHandler | None = None,
        config: ClientConfig | None = None,
        signer: CookieSigner | None = None,
        token_provider: WsTokenProvider | None = None,
        account_lock: AccountConnectionLock | None = None,
    ) -> None:
        self.account_id = account_id
        self.config = config or ClientConfig.from_settings()
        self.signer = signer or CookieSigner()
        self.token_provider = token_provider or WsTokenProvider(self.signer)
        self._account_lock = account_lock or AccountConnectionLock(
            get_settings().account_lock_path(account_id)
        )
        self.on_event = on_event
        self.on_state = on_state
        self.on_error = on_error
        self.on_auth_failure = on_auth_failure
        self.on_frame = on_frame
        self._state = ConnectionState.IDLE
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inject_task: asyncio.Task[None] | None = None
        self._socket: Any | None = None
        self._injected_queue: asyncio.Queue[WsFrame] | None = None
        self._reconnect_attempts = 0
        self._account_user_id: str | None = None
        self._account_lock_held = False

        self._router: ws_request_router.RequestRouter | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._protocol_tasks: set[asyncio.Task[None]] = set()
        self._protocol_error: Exception | None = None
        self._subscription_ready: ws_sync.SubscriptionReady | None = None
        self._outbound_ready = False
        self._business_dispatch_ready = True
        self._business_session_ready = asyncio.Event()
        self._business_session_ready.set()
        self._deferred_business_frames: deque[DeferredBusinessEvents] = deque()
        self._deferred_business_slots = asyncio.BoundedSemaphore(MAX_CALLBACK_QUEUE_SIZE)
        self._retained_business_drain_task: asyncio.Task[None] | None = None
        self._deferred_sync_frames: deque[WsFrame] = deque()
        self._deferred_sync_slots = asyncio.BoundedSemaphore(MAX_DEFERRED_SYNC_FRAMES)
        self._dispatch_queue: asyncio.Queue[DispatchCallback] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._dispatch_inflight = False
        self._dispatch_stop_when_drained = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def subscription_ready(self) -> ws_sync.SubscriptionReady | None:
        """Latest protocol-ready marker for the active connection, if any."""
        return self._subscription_ready

    async def _emit_state(self, new_state: ConnectionState, detail: str | None = None) -> None:
        self._state = new_state
        event = ConnectionStateChanged(
            event_id=uuid.uuid4().hex,
            account_id=self.account_id,
            state=new_state,
            detail=detail,
            occurred_at=datetime.now(UTC),
        )
        await self._update_worker_status(state=new_state, detail=detail)
        if self.on_state is not None:
            with suppress(Exception):
                await self.on_state(event)

    async def _emit_error(self, code: str, message: str) -> None:
        event = ErrorOccurred(
            event_id=uuid.uuid4().hex,
            account_id=self.account_id,
            code=code,
            message=message,
            occurred_at=datetime.now(UTC),
        )
        if self.on_error is not None:
            with suppress(Exception):
                await self.on_error(event)

    async def _emit_event(self, event: EventEnvelope) -> None:
        if self.on_event is None:
            return
        with suppress(Exception):
            await self.on_event(event)

    async def _update_worker_status(self, *, state: ConnectionState, detail: str | None) -> None:
        """Persist the existing legacy connection-state projection."""
        try:
            async with get_async_session() as session:
                stmt = select(Account).where(Account.account_id == self.account_id).limit(1)
                account = (await session.execute(stmt)).scalar_one_or_none()
                if account is None:
                    return
                stmt_ws = (
                    select(DbWorkerStatus).where(DbWorkerStatus.account_id == account.id).limit(1)
                )
                row = (await session.execute(stmt_ws)).scalar_one_or_none()
                if row is None:
                    row = DbWorkerStatus(account_id=account.id, status=state)
                    session.add(row)
                elif state == ConnectionState.DISCONNECTED and row.risk_recovery_required:
                    await session.commit()
                    return
                else:
                    row.status = state
                if state == ConnectionState.CONNECTED:
                    row.reconnect_attempts = self._reconnect_attempts
                    row.last_heartbeat_at = datetime.now(UTC)
                    row.started_at = row.started_at or datetime.now(UTC)
                    row.last_error = None
                if detail and state in {ConnectionState.ERROR, ConnectionState.DISCONNECTED}:
                    row.last_error = detail
                await session.commit()
        except Exception as exc:
            logger.warning("worker_status update failed: %s", exc)

    def start(self) -> None:
        """Spawn the background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._account_lock.acquire(owner_id=f"ws:{self.account_id}:{uuid.uuid4().hex}")
        self._account_lock_held = True
        self._stop.clear()
        try:
            if self._injected_queue is None:
                self._injected_queue = asyncio.Queue()
            self._start_dispatch_consumer()
            self._inject_task = asyncio.create_task(
                self._pump_injected(), name=f"ws-inject-{self.account_id}"
            )
            self._task = asyncio.create_task(self._run_forever(), name=f"ws-{self.account_id}")
        except Exception:
            if self._dispatch_task is not None and not self._dispatch_task.done():
                self._dispatch_task.cancel()
            self._dispatch_queue = None
            self._dispatch_task = None
            self._release_account_lock()
            raise

    async def _drain_injected(self) -> None:
        if self._inject_task is None:
            return
        self._inject_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._inject_task
        self._inject_task = None

    async def stop(self) -> None:
        """Request clean shutdown within the configured stop deadline."""
        self._stop.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, self.config.stop_timeout_s)
        if self._task is not None:
            if self._socket is not None:
                with suppress(Exception):
                    await self._socket.close()
            remaining = max(0.0, deadline - loop.time())
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=remaining)
            except TimeoutError:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        await self._drain_injected()

        remaining = max(0.0, deadline - loop.time())
        if self._deferred_business_frames and remaining > 0.0:
            try:
                await asyncio.wait_for(
                    self._flush_deferred_business_frames(), timeout=remaining
                )
            except TimeoutError:
                logger.warning(
                    "ws deferred business drain timed out account=%s remaining=%d",
                    self.account_id,
                    len(self._deferred_business_frames),
                )

        retained_drain_active = False
        if self._deferred_business_frames:
            self._ensure_retained_business_drain()
            retained_drain_active = self._retained_business_drain_active()

        remaining = max(0.0, deadline - loop.time())
        if not retained_drain_active:
            if remaining > 0.0:
                try:
                    await asyncio.wait_for(
                        self._stop_dispatch_consumer(drain=True), timeout=remaining
                    )
                except TimeoutError:
                    logger.warning("ws callback drain timed out account=%s", self.account_id)
                    self._request_dispatch_stop_when_drained()
            else:
                self._request_dispatch_stop_when_drained()
        await self._emit_state(ConnectionState.DISCONNECTED, "stop() called")

    def inject_frame(self, frame: WsFrame) -> None:
        """Schedule a synthetic frame for the legacy callback/parser path."""
        if self._injected_queue is None:
            self._injected_queue = asyncio.Queue()
        self._injected_queue.put_nowait(frame)

    async def send_text(self, text: str) -> bool:
        """Send an outbound text frame. Returns False until the session is ready."""
        if (
            not self._business_session_ready.is_set()
            or not self._outbound_ready
            or self._socket is None
        ):
            return False
        return await ws_sender.send_text(self._socket, text)

    async def request_conversations(
        self,
        *,
        cursor: int = 0,
        limit: int = 0,
        timeout_s: float | None = ws_history.DEFAULT_TIMEOUT_S,
    ) -> Any:
        """Request the conversation list through the active shared router."""
        ws, router = self._require_protocol_context()
        return await ws_history.request_conversations(
            router,
            lambda frame: self._send_protocol_frame(ws, frame),
            cursor=cursor,
            limit=limit,
            timeout_s=timeout_s,
        )

    async def request_message_history(
        self,
        cid: str,
        *,
        cursor: int = 0,
        limit: int = 0,
        timeout_s: float | None = ws_history.DEFAULT_TIMEOUT_S,
    ) -> Any:
        """Request one conversation's message history through the shared router."""
        ws, router = self._require_protocol_context()
        return await ws_history.request_message_history(
            router,
            lambda frame: self._send_protocol_frame(ws, frame),
            cid=cid,
            cursor=cursor,
            limit=limit,
            timeout_s=timeout_s,
        )

    def _require_protocol_context(self) -> tuple[Any, ws_request_router.RequestRouter]:
        if (
            not self._business_session_ready.is_set()
            or not self._business_dispatch_ready
            or not self._outbound_ready
            or self._socket is None
            or self._router is None
        ):
            msg = "WebSocket protocol session is not connected"
            raise ConnectionError(msg)
        return self._socket, self._router

    def _set_business_session_ready(self, ready: bool) -> None:
        """Open or close all business-facing session capabilities atomically."""
        if ready:
            self._outbound_ready = True
            self._business_dispatch_ready = True
            self._business_session_ready.set()
            return
        self._business_session_ready.clear()
        self._business_dispatch_ready = False
        self._outbound_ready = False

    async def _run_forever(self) -> None:
        """Main loop: connect -> heartbeat+recv -> disconnect -> bounded retry."""
        try:
            while not self._stop.is_set():
                if not self.config.ws_url:
                    await self._emit_state(ConnectionState.ERROR, "XIANYU_WS_URL not configured")
                    await self._sleep_or_stop(5.0)
                    continue
                retry_delay: float | None = None
                try:
                    await self._connect_and_serve()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._reconnect_attempts += 1
                    msg = f"{type(exc).__name__}: {exc}"
                    logger.warning("ws loop error account=%s %s", self.account_id, msg)
                    auth_failure = isinstance(exc, WsAuthError)
                    new_state = (
                        ConnectionState.ERROR
                        if auth_failure or self._reconnect_attempts >= 5
                        else ConnectionState.RECONNECTING
                    )
                    if auth_failure:
                        retry_delay = self.config.auth_retry_delay_s
                    await self._emit_state(new_state, msg)
                    await self._emit_error("ws_auth" if auth_failure else "ws_loop", msg)
                    if auth_failure and self.on_auth_failure is not None:
                        try:
                            stop_retry = await self.on_auth_failure(exc)
                        except Exception as handler_exc:
                            logger.warning(
                                "ws auth failure handler failed account=%s error=%s",
                                self.account_id,
                                handler_exc,
                            )
                        else:
                            if stop_retry:
                                break
                if self._stop.is_set():
                    break
                backoff = retry_delay if retry_delay is not None else self._compute_backoff()
                await self._sleep_or_stop(backoff)
        finally:
            await self._emit_state(ConnectionState.DISCONNECTED, "loop exited")
            self._release_account_lock()

    def _release_account_lock(self) -> None:
        if not self._account_lock_held:
            return
        self._account_lock.release()
        self._account_lock_held = False

    def _compute_backoff(self) -> float:
        base = min(
            self.config.max_backoff_s,
            self.config.min_backoff_s * (2 ** min(self._reconnect_attempts, 6)),
        )
        return base + random.uniform(0, 0.5 * base)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def _connect_and_serve(self) -> None:  # noqa: PLR0915
        """Open one connection and establish one receive owner for its lifetime."""
        owns_dispatch_consumer = self._dispatch_task is None or self._dispatch_task.done()
        if owns_dispatch_consumer:
            self._start_dispatch_consumer()
        self._account_user_id = None
        self._subscription_ready = None
        self._set_business_session_ready(False)
        await self._emit_state(ConnectionState.CONNECTING)
        credentials = await self.token_provider.get_credentials(self.account_id)
        cookie_value = await self.signer.load_cookie_value(self.account_id)
        if not cookie_value:
            msg = f"no cookie for account={self.account_id}"
            raise RuntimeError(msg)

        try:
            async with ws_connector.open_connection(self.config.ws_url, cookie_value) as ws:
                router = ws_request_router.RequestRouter()
                self._account_user_id = credentials.user_id
                self._socket = ws
                self._router = router
                receive_task = asyncio.create_task(
                    self._receive_loop(ws, router),
                    name=f"ws-receive-{self.account_id}",
                )
                self._receive_task = receive_task
                self._protocol_error = None
                try:
                    await self._register_and_sync(
                        ws,
                        credentials,
                        router=router,
                        receive_task=receive_task,
                    )
                    if self._stop.is_set():
                        return
                    self._raise_if_receive_owner_finished(receive_task)
                    self._set_business_session_ready(True)
                    await self._flush_deferred_business_frames()
                    if self._stop.is_set():
                        return
                    self._start_deferred_sync_exchanges(ws, router)
                    self._reconnect_attempts = 0
                    await self._emit_state(ConnectionState.CONNECTED)
                    await self._serve(ws, receive_task)
                finally:
                    self._set_business_session_ready(False)
                    await self._cancel_protocol_tasks()
                    router.close()
                    if not receive_task.done():
                        receive_task.cancel()
                    try:
                        with suppress(asyncio.CancelledError):
                            await receive_task
                    finally:
                        self._receive_task = None
                        self._router = None
                        self._socket = None
        finally:
            self._set_business_session_ready(False)
            self._subscription_ready = None
            self._account_user_id = None
            if owns_dispatch_consumer:
                await self._stop_dispatch_consumer(drain=True)

    @staticmethod
    def _raise_if_receive_owner_finished(receive_task: asyncio.Task[None]) -> None:
        if not receive_task.done():
            return
        if receive_task.cancelled():
            raise asyncio.CancelledError
        receive_error = receive_task.exception()
        if receive_error is not None:
            raise receive_error
        msg = "WebSocket closed before business dispatch became ready"
        raise ConnectionError(msg)

    async def _register_and_sync(
        self,
        ws: Any,
        credentials: Any,
        *,
        router: ws_request_router.RequestRouter,
        receive_task: asyncio.Task[None],
    ) -> None:
        """Register through the shared router; this method never reads the socket."""
        registration = build_registration_frame(credentials)
        reg_mid = str(registration["headers"]["mid"])
        pending = router.register(reg_mid)
        try:
            await ws.send(json.dumps(registration))
        except asyncio.CancelledError:
            router.cancel(reg_mid)
            raise
        except Exception:
            router.cancel(reg_mid)
            raise

        waiter = asyncio.create_task(
            router.wait(pending, timeout_s=self.config.registration_timeout_s),
            name=f"ws-register-wait-{self.account_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {waiter, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            router.cancel(reg_mid)
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
            raise

        if waiter in done:
            try:
                decoded = await waiter
            except TimeoutError:
                msg = "IM registration response timeout"
                raise TimeoutError(msg) from None
        else:
            router.cancel(reg_mid)
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
            if receive_task.cancelled():
                raise asyncio.CancelledError
            receive_error = receive_task.exception()
            if receive_error is not None:
                raise receive_error
            msg = "WebSocket closed during IM registration"
            raise ConnectionError(msg)

        data = decoded.payload
        code = int(data.get("code", 200)) if isinstance(data, dict) else 200
        if code != 200:
            msg = f"IM registration rejected code={code}"
            raise RuntimeError(msg)

        await self._sleep_or_stop(self.config.registration_delay_s)
        if self._stop.is_set():
            return
        await ws.send(json.dumps(build_sync_frame()))

    async def _receive_loop(
        self,
        ws: Any,
        router: ws_request_router.RequestRouter,
    ) -> None:
        """The sole network receive owner for one WebSocket connection."""
        owns_dispatch_consumer = self._dispatch_task is None or self._dispatch_task.done()
        if owns_dispatch_consumer:
            self._start_dispatch_consumer()
        try:
            async for raw in ws:
                decoded = ws_decoder.decode_frame(raw)
                if decoded is None:
                    raw_text = ws_decoder.normalize_frame_text(raw)
                    logger.debug("non-JSON frame skipped: length=%d", len(raw_text))
                    continue

                frame = decoded.frame
                await self._handle_or_defer_business_frame(frame)
                reserve_sync = ws_sync.requires_state_sync(frame)
                sync_slot_owned = False
                if reserve_sync:
                    await self._deferred_sync_slots.acquire()
                    sync_slot_owned = True
                try:
                    matched = router.match_frame(frame, decoded)
                    await ws_ack.send_ack(ws, frame)
                    if sync_slot_owned and not matched:
                        deferred = self._handle_or_defer_sync_frame(ws, router, frame)
                        if not deferred:
                            self._deferred_sync_slots.release()
                        sync_slot_owned = False
                finally:
                    if sync_slot_owned:
                        self._deferred_sync_slots.release()
        finally:
            if owns_dispatch_consumer:
                await self._stop_dispatch_consumer(drain=True)

    def _handle_or_defer_sync_frame(
        self,
        ws: Any,
        router: ws_request_router.RequestRouter,
        frame: WsFrame,
    ) -> bool:
        if self._business_session_ready.is_set():
            self._start_sync_exchange(ws, router, frame)
            return False
        self._deferred_sync_frames.append(frame)
        return True

    def _start_deferred_sync_exchanges(
        self,
        ws: Any,
        router: ws_request_router.RequestRouter,
    ) -> None:
        while self._deferred_sync_frames:
            frame = self._deferred_sync_frames.popleft()
            try:
                self._start_sync_exchange(ws, router, frame)
            except Exception:
                self._deferred_sync_frames.appendleft(frame)
                raise
            else:
                self._deferred_sync_slots.release()

    def _start_sync_exchange(
        self,
        ws: Any,
        router: ws_request_router.RequestRouter,
        frame: WsFrame,
    ) -> None:
        task = asyncio.create_task(
            self._run_sync_exchange(ws, router, frame),
            name=f"ws-sync-state-{self.account_id}",
        )
        self._protocol_tasks.add(task)
        task.add_done_callback(self._discard_protocol_task)

    def _discard_protocol_task(self, task: asyncio.Task[None]) -> None:
        self._protocol_tasks.discard(task)

    async def _run_sync_exchange(
        self,
        ws: Any,
        router: ws_request_router.RequestRouter,
        frame: WsFrame,
    ) -> None:
        try:
            ready = await ws_sync.handle_sync_extra(
                router,
                lambda request: self._send_protocol_frame(ws, request),
                frame,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._protocol_error = exc
            with suppress(Exception):
                await ws.close()
            return

        if ready is not None:
            self._subscription_ready = ready

    async def _send_protocol_frame(self, ws: Any, frame: dict[str, Any]) -> bool:
        try:
            await ws.send(json.dumps(frame))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True

    async def _cancel_protocol_tasks(self) -> None:
        if not self._protocol_tasks:
            return
        tasks = tuple(self._protocol_tasks)
        self._protocol_tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start_dispatch_consumer(self) -> None:
        self._dispatch_stop_when_drained = False
        if self._dispatch_task is not None and not self._dispatch_task.done():
            return
        queue: asyncio.Queue[DispatchCallback] = asyncio.Queue(
            maxsize=MAX_CALLBACK_QUEUE_SIZE
        )
        self._dispatch_queue = queue
        self._dispatch_inflight = False
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(queue),
            name=f"ws-dispatch-{self.account_id}",
        )

    async def _stop_dispatch_consumer(self, *, drain: bool) -> None:
        queue = self._dispatch_queue
        task = self._dispatch_task
        if queue is None or task is None:
            self._dispatch_queue = None
            self._dispatch_task = None
            return
        if drain and not task.done():
            await queue.join()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if self._dispatch_task is task:
            self._dispatch_task = None
        if self._dispatch_queue is queue:
            self._dispatch_queue = None
        self._dispatch_inflight = False

    def _request_dispatch_stop_when_drained(self) -> None:
        queue = self._dispatch_queue
        task = self._dispatch_task
        if queue is None or task is None:
            self._dispatch_queue = None
            self._dispatch_task = None
            self._dispatch_inflight = False
            return
        self._dispatch_stop_when_drained = True
        if task.done():
            self._dispatch_task = None
            if queue.empty():
                self._dispatch_queue = None
            self._dispatch_inflight = False
            return
        if queue.empty() and not self._dispatch_inflight:
            task.cancel()

    async def _dispatch_loop(self, queue: asyncio.Queue[DispatchCallback]) -> None:
        current = asyncio.current_task()
        try:
            while True:
                if self._dispatch_stop_when_drained and queue.empty():
                    return
                callback = await queue.get()
                self._dispatch_inflight = True
                try:
                    await callback()
                except asyncio.CancelledError:
                    if current is not None and current.cancelling():
                        raise
                    logger.warning("ws callback cancelled account=%s", self.account_id)
                except Exception as exc:
                    logger.warning(
                        "ws callback dispatch failed account=%s error=%s",
                        self.account_id,
                        exc,
                    )
                finally:
                    self._dispatch_inflight = False
                    queue.task_done()
                if self._dispatch_stop_when_drained and queue.empty():
                    return
        finally:
            self._dispatch_inflight = False
            if self._dispatch_task is current:
                self._dispatch_task = None
            if self._dispatch_queue is queue and queue.empty():
                self._dispatch_queue = None

    async def _queue_dispatch(self, callback: DispatchCallback) -> None:
        queue = self._dispatch_queue
        if queue is None:
            msg = "WebSocket callback dispatcher is not running"
            raise RuntimeError(msg)
        await queue.put(callback)

    async def _queue_frame_callback(self, frame: WsFrame) -> None:
        if self.on_frame is None:
            return

        async def dispatch_frame() -> None:
            await self._emit_frame_callback(frame)

        await self._queue_dispatch(dispatch_frame)

    async def _queue_events(self, events: tuple[EventEnvelope, ...]) -> None:
        if not events or self.on_event is None:
            return

        async def dispatch_events() -> None:
            await self._dispatch_events(events, require_session_ready=True)

        await self._queue_dispatch(dispatch_events)

    async def _wait_for_business_session_or_stop(self) -> None:
        if self._business_session_ready.is_set() or self._stop.is_set():
            return
        ready_waiter = asyncio.create_task(self._business_session_ready.wait())
        stop_waiter = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait(
                {ready_waiter, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in (ready_waiter, stop_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(ready_waiter, stop_waiter, return_exceptions=True)

    async def _serve(self, ws: Any, receive_task: asyncio.Task[None]) -> None:
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(ws),
            name=f"ws-heartbeat-{self.account_id}",
        )
        try:
            await receive_task
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        if self._protocol_error is not None:
            error = self._protocol_error
            self._protocol_error = None
            raise error

    async def _heartbeat_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                await ws.send(self.config.heartbeat_builder())
            except Exception as exc:
                logger.debug("heartbeat send failed: %s", exc)
                return
            await self._touch_heartbeat()
            try:
                await self._sleep_or_stop(self.config.heartbeat_interval_s)
            except asyncio.CancelledError:
                return

    async def _touch_heartbeat(self) -> None:
        """Persist last_heartbeat_at so other processes can observe liveness."""
        try:
            async with get_async_session() as session:
                stmt = (
                    select(DbWorkerStatus)
                    .join(Account, Account.id == DbWorkerStatus.account_id)
                    .where(Account.account_id == self.account_id)
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return
                row.last_heartbeat_at = datetime.now(UTC)
                await session.commit()
        except Exception as exc:
            logger.debug("heartbeat touch failed: %s", exc)

    async def _pump_injected(self) -> None:
        while not self._stop.is_set():
            if self._injected_queue is None:
                return
            try:
                frame = await asyncio.wait_for(self._injected_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._handle_frame(frame)
            except Exception as exc:
                logger.warning("inject handler failed: %s", exc)

    async def _handle_raw(self, raw: str | bytes) -> None:
        """Compatibility helper for directly supplied raw frames."""
        decoded = ws_decoder.decode_frame(raw)
        if decoded is None:
            raw_text = ws_decoder.normalize_frame_text(raw)
            logger.debug("non-JSON frame skipped: length=%d", len(raw_text))
            return
        await self._ack_and_handle(self._socket, decoded.frame)

    async def _ack_and_handle(self, ws: Any | None, frame: WsFrame) -> None:
        await ws_ack.send_ack(ws, frame)
        await self._handle_frame(frame)

    def _parse_frame_events(self, frame: WsFrame) -> tuple[EventEnvelope, ...]:
        return tuple(
            parse_frame(
                frame,
                self.account_id,
                account_user_id=self._account_user_id,
            )
        )

    async def _emit_frame_callback(self, frame: WsFrame) -> None:
        if self.on_frame is not None:
            with suppress(Exception):
                await self.on_frame(frame)

    async def _dispatch_events(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        require_session_ready: bool = False,
    ) -> None:
        for event in events:
            if require_session_ready:
                await self._wait_for_business_session_or_stop()
            try:
                await self._emit_event(event)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                logger.warning(
                    "ws event callback cancelled account=%s event=%s",
                    self.account_id,
                    type(event).__name__,
                )

    async def _handle_or_defer_business_frame(self, frame: WsFrame) -> None:
        await self._queue_frame_callback(frame)
        events = self._parse_frame_events(frame)
        if not self._business_session_ready.is_set() and events and self.on_event is not None:
            await self._deferred_business_slots.acquire()
            if self._business_session_ready.is_set():
                self._deferred_business_slots.release()
                await self._queue_events(events)
            else:
                self._deferred_business_frames.append(events)
            return
        await self._queue_events(events)

    async def _flush_deferred_business_frames(self) -> None:
        if self._business_session_ready.is_set():
            self._business_dispatch_ready = True
        while self._deferred_business_frames:
            events = self._deferred_business_frames[0]
            await self._queue_events(events)
            self._deferred_business_frames.popleft()
            self._deferred_business_slots.release()

    def _retained_business_drain_active(self) -> bool:
        task = self._retained_business_drain_task
        return task is not None and not task.done()

    def _ensure_retained_business_drain(self) -> None:
        if not self._deferred_business_frames or self._retained_business_drain_active():
            return
        self._retained_business_drain_task = asyncio.create_task(
            self._drain_retained_business_after_stop(),
            name=f"ws-retained-business-drain-{self.account_id}",
        )

    async def _drain_retained_business_after_stop(self) -> None:
        current = asyncio.current_task()
        try:
            self._start_dispatch_consumer()
            await self._flush_deferred_business_frames()
            queue = self._dispatch_queue
            if queue is not None:
                await queue.join()
            await self._stop_dispatch_consumer(drain=False)
        finally:
            if self._retained_business_drain_task is current:
                self._retained_business_drain_task = None

    async def _handle_frame(self, frame: WsFrame) -> None:
        await self._emit_frame_callback(frame)
        await self._dispatch_events(self._parse_frame_events(frame))
