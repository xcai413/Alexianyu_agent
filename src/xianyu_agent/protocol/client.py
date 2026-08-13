"""Single-account WebSocket client.

Wraps `websockets` with:
  - exponential backoff reconnect
  - heartbeat (configurable interval, default 30s)
  - frame dispatch to a user-supplied callback
  - clean shutdown on demand

The class is intentionally stateful (one instance per account) so it can be
owned by the future `AccountPool` in Phase 2 without surprising side effects.

URL comes from settings.ws_url (env `XIANYU_WS_URL`). If empty, the client
runs in offline mode: frames can still be fed programmatically via
`inject_frame`, useful for tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
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
from xianyu_agent.protocol.parser import build_ack_frame, parse_frame
from xianyu_agent.protocol.signer import CookieSigner
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
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


def _generate_mid() -> str:
    """闲鱼消息 ID 格式:<随机3位><毫秒时间戳> 0"""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def _default_heartbeat() -> str:
    """闲鱼 WS 心跳帧:lwp/! + mid(与真实服务对齐)。"""
    return json.dumps({"lwp": "/!", "headers": {"mid": _generate_mid()}})


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
        s = get_settings()
        return cls(ws_url=s.ws_url)


class WsClient:
    """Single-account WS client.
    Public surface:
      start(): begin reconnect loop
      stop(): clean shutdown
      inject_frame(frame): push a synthetic frame (testing)
      send_text(text): outbound message (used by reply engine later)
      state: current connection state
    """

    def __init__(
        self,
        account_id: str,
        *,
        on_event: EventHandler | None = None,
        on_state: StateHandler | None = None,
        on_error: ErrorHandler | None = None,
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
        self.on_frame = on_frame
        self._state = ConnectionState.IDLE
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._socket: Any | None = None
        self._injected_queue: asyncio.Queue | None = None
        self._reconnect_attempts = 0
        self._account_user_id: str | None = None
        self._account_lock_held = False

    @property
    def state(self) -> ConnectionState:
        return self._state

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
        """Persist current state to the worker_status table."""
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
            self._inject_task = asyncio.create_task(
                self._pump_injected(), name=f"ws-inject-{self.account_id}"
            )
            self._task = asyncio.create_task(self._run_forever(), name=f"ws-{self.account_id}")
        except Exception:
            self._release_account_lock()
            raise

    async def _drain_injected(self) -> None:
        """Cancel inject task."""
        if hasattr(self, "_inject_task") and self._inject_task is not None:
            self._inject_task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await self._inject_task

    async def stop(self) -> None:
        """Request clean shutdown and wait for the task to exit."""
        self._stop.set()
        if self._task is not None:
            if self._socket is not None:
                with suppress(Exception):
                    await self._socket.close()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._task), timeout=self.config.stop_timeout_s
                )
            except TimeoutError:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        await self._drain_injected()
        await self._emit_state(ConnectionState.DISCONNECTED, "stop() called")

    def inject_frame(self, frame: WsFrame) -> None:
        """Schedule the frame to be processed by the running loop.
        Tests use this to drive the client without a real network.
        """
        if self._injected_queue is None:
            self._injected_queue = asyncio.Queue()
        self._injected_queue.put_nowait(frame)

    async def send_text(self, text: str) -> bool:
        """Send an outbound text frame. Returns False if not connected."""
        if self._socket is None:
            return False
        with suppress(Exception):
            await self._socket.send(text)
            return True
        return False

    async def _run_forever(self) -> None:
        """Main loop: connect -> heartbeat+recv -> on disconnect, backoff + retry."""
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

    async def _connect_and_serve(self) -> None:
        """Open one connection, serve until it drops."""
        await self._emit_state(ConnectionState.CONNECTING)
        try:
            import websockets  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            msg = "websockets package is required for live connections"
            raise RuntimeError(msg) from exc
        credentials = await self.token_provider.get_credentials(self.account_id)
        cookie_value = await self.signer.load_cookie_value(self.account_id)
        if not cookie_value:
            msg = f"no cookie for account={self.account_id}"
            raise RuntimeError(msg)
        additional_headers = [("Cookie", cookie_value)]
        async with websockets.connect(
            self.config.ws_url,
            additional_headers=additional_headers,
            ping_interval=None,
            ping_timeout=None,
        ) as ws:
            await self._register_and_sync(ws, credentials)
            self._socket = ws
            self._account_user_id = credentials.user_id
            self._reconnect_attempts = 0
            await self._emit_state(ConnectionState.CONNECTED)
            await self._serve(ws)

    async def _register_and_sync(self, ws: Any, credentials: Any) -> None:
        """Require the matching `/reg` response before declaring the session connected."""
        registration = build_registration_frame(credentials)
        reg_mid = str(registration["headers"]["mid"])
        await ws.send(json.dumps(registration))
        deadline = time.monotonic() + self.config.registration_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = "IM registration response timeout"
                raise TimeoutError(msg)
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            frame = WsFrame.model_validate(data) if isinstance(data, dict) else WsFrame(body=raw)
            headers = frame.headers if isinstance(frame.headers, dict) else {}
            if str(headers.get("mid") or "") == reg_mid:
                ack = build_ack_frame(frame)
                if ack is not None:
                    await ws.send(json.dumps(ack))
                await self._handle_frame(frame)
                code = int(data.get("code", 200)) if isinstance(data, dict) else 200
                if code != 200:
                    msg = f"IM registration rejected code={code}"
                    raise RuntimeError(msg)
                break
            await self._ack_and_handle(ws, frame)
        await self._sleep_or_stop(self.config.registration_delay_s)
        if self._stop.is_set():
            return
        await ws.send(json.dumps(build_sync_frame()))

    async def _serve(self, ws: Any) -> None:
        if self._injected_queue is None:
            self._injected_queue = asyncio.Queue()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws), name="ws-heartbeat")
        inject_task = asyncio.create_task(self._pump_injected(), name="ws-inject")
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")  # noqa: PLW2901
                await self._handle_raw(raw)
        finally:
            heartbeat_task.cancel()
            inject_task.cancel()
            for t in (heartbeat_task, inject_task):
                with suppress(asyncio.CancelledError):
                    await t
            self._socket = None

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

    async def _handle_raw(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("non-JSON frame skipped: length=%d", len(raw))
            return
        frame = WsFrame.model_validate(data) if isinstance(data, dict) else WsFrame(body=raw)
        await self._ack_and_handle(self._socket, frame)

    async def _ack_and_handle(self, ws: Any | None, frame: WsFrame) -> None:
        ack = build_ack_frame(frame)
        if ack is not None and ws is not None:
            await ws.send(json.dumps(ack))
        await self._handle_frame(frame)

    async def _handle_frame(self, frame: WsFrame) -> None:
        if self.on_frame is not None:
            with suppress(Exception):
                await self.on_frame(frame)
        for event in parse_frame(
            frame, self.account_id, account_user_id=self._account_user_id
        ):
            await self._emit_event(event)
