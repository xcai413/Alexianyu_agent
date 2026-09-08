"""Transport-neutral WebSocket request/response rendezvous primitive."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from xianyu_agent.protocol.events import WsFrame


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """Handle for one registered request awaiting a correlated response."""

    request_id: str
    future: asyncio.Future[Any]


def request_id_from_frame(frame: WsFrame) -> str | None:
    """Return the response correlation id from a frame, if one is present."""
    headers = frame.headers if isinstance(frame.headers, dict) else {}
    value = headers.get("mid")
    if value is None:
        return None
    request_id = str(value)
    return request_id or None


class RequestRouter:
    """Match concurrent protocol responses to pending asyncio waiters.

    The router owns only rendezvous state. It does not read from or write to a
    transport and has no database, credential, or domain dependencies.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingRequest] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        """Number of requests that have not been resolved or cancelled."""
        return len(self._pending)

    @property
    def closed(self) -> bool:
        return self._closed

    def register(self, request_id: str) -> PendingRequest:
        """Register one request id before its response can arrive."""
        if self._closed:
            msg = "request router is closed"
            raise RuntimeError(msg)
        if not request_id:
            msg = "request_id must not be empty"
            raise ValueError(msg)
        if request_id in self._pending:
            msg = f"request_id already pending: {request_id}"
            raise ValueError(msg)

        future = asyncio.get_running_loop().create_future()
        pending = PendingRequest(request_id=request_id, future=future)
        self._pending[request_id] = pending

        def cleanup(done: asyncio.Future[Any]) -> None:
            self._discard_if_current(request_id, done)

        future.add_done_callback(cleanup)
        return pending

    def match(self, request_id: str, response: Any) -> bool:
        """Resolve a pending request; return False for unknown or late responses."""
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(response)
        return True

    def match_frame(self, frame: WsFrame, response: Any) -> bool:
        """Resolve by the frame ``headers.mid`` correlation id when available."""
        request_id = request_id_from_frame(frame)
        if request_id is None:
            return False
        return self.match(request_id, response)

    def fail(self, request_id: str, exc: Exception) -> bool:
        """Fail a pending request when its surrounding receive operation fails."""
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_exception(exc)
        return True

    def cancel(self, request_id: str) -> bool:
        """Cancel one pending request and remove its rendezvous state."""
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        if not pending.future.done():
            pending.future.cancel()
        return True

    async def wait(self, pending: PendingRequest, *, timeout_s: float | None) -> Any:
        """Wait for a registered response, cleaning state on timeout/cancellation."""
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=timeout_s)
        except TimeoutError:
            self._cancel_pending(pending)
            raise
        except asyncio.CancelledError:
            self._cancel_pending(pending)
            raise
        finally:
            self._discard_if_current(pending.request_id, pending.future)

    def close(self) -> None:
        """Stop accepting requests and cancel every unresolved waiter."""
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.cancel()

    def _cancel_pending(self, pending: PendingRequest) -> None:
        current = self._pending.get(pending.request_id)
        if current is not pending:
            return
        self._pending.pop(pending.request_id, None)
        if not pending.future.done():
            pending.future.cancel()

    def _discard_if_current(self, request_id: str, future: asyncio.Future[Any]) -> None:
        current = self._pending.get(request_id)
        if current is not None and current.future is future:
            self._pending.pop(request_id, None)
