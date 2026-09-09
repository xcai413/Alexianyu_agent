"""Targeted regressions for WS callback lifecycle reliability."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from xianyu_agent.protocol.events import EventEnvelope, WsFrame
from xianyu_agent.protocol.ws import client as ws_client_module, request_router

_CLOSE = object()


class _NoopLock:
    def acquire(self, *, owner_id: str) -> None:
        pass

    def release(self) -> None:
        pass


class _SendSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class _ReceiveSocket(_SendSocket):
    def __init__(self) -> None:
        super().__init__()
        self.inbound: asyncio.Queue[str | object] = asyncio.Queue()
        self.iterator_count = 0
        self.recv_called = False

    async def recv(self) -> str:
        self.recv_called = True
        raise AssertionError("reliability regressions must preserve the single receive owner")

    def __aiter__(self) -> _ReceiveSocket:
        self.iterator_count += 1
        if self.iterator_count > 1:
            raise AssertionError("more than one receive iterator owns the socket")
        return self

    async def __anext__(self) -> str:
        item = await self.inbound.get()
        if item is _CLOSE:
            raise StopAsyncIteration
        assert isinstance(item, str)
        return item

    def push(self, payload: dict[str, Any]) -> None:
        self.inbound.put_nowait(json.dumps(payload))

    async def close(self) -> None:
        self.inbound.put_nowait(_CLOSE)


async def _noop_emit_state(*args: Any, **kwargs: Any) -> None:
    return None


def _event(event_id: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        account_id="acc-client",
        received_at=datetime.now(UTC),
    )


def _make_client(*, on_event=None, stop_timeout_s: float = 0.2) -> ws_client_module.WsClient:
    return ws_client_module.WsClient(
        "acc-client",
        on_event=on_event,
        config=ws_client_module.ClientConfig(
            ws_url="",
            registration_delay_s=0.0,
            stop_timeout_s=stop_timeout_s,
        ),
        signer=object(),
        token_provider=object(),
        account_lock=_NoopLock(),
    )


async def _wait_until(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def _acked_mids(socket: _SendSocket) -> list[str]:
    mids: list[str] = []
    for payload in socket.sent:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if value.get("code") != 200:
            continue
        mid = value.get("headers", {}).get("mid")
        if mid is not None:
            mids.append(str(mid))
    return mids


@pytest.mark.asyncio
async def test_history_cancellation_isolated_per_event_and_remaining_event_waits_reconnect() -> None:
    first = _event("first")
    second = _event("second")
    seen: list[str] = []
    router = request_router.RequestRouter()
    socket = _SendSocket()
    client: ws_client_module.WsClient

    async def on_event(event: EventEnvelope) -> None:
        seen.append(event.event_id)
        if event is first:
            await client.request_conversations(timeout_s=None)

    client = _make_client(on_event=on_event)
    client._start_dispatch_consumer()
    client._socket = socket
    client._router = router
    client._set_business_session_ready(True)

    await client._queue_events((first, second))
    await _wait_until(lambda: router.pending_count == 1)

    # Reproduce connection teardown ordering: pause future business callbacks first,
    # then close the router so the first event's history waiter is cancelled.
    client._set_business_session_ready(False)
    router.close()

    await _wait_until(lambda: seen == ["first"])
    for _ in range(20):
        await asyncio.sleep(0)
    assert seen == ["first"]
    assert client._dispatch_task is not None
    assert client._dispatch_task.done() is False

    # A new ready session releases only the remaining event from the same ACKed batch.
    client._socket = _SendSocket()
    client._router = request_router.RequestRouter()
    client._set_business_session_ready(True)

    assert client._dispatch_queue is not None
    await client._dispatch_queue.join()
    assert seen == ["first", "second"]
    await client._stop_dispatch_consumer(drain=True)


@pytest.mark.asyncio
async def test_queued_business_callback_waits_for_reconnect_session_ready() -> None:
    event = _event("retained")
    seen: list[str] = []

    async def on_event(value: EventEnvelope) -> None:
        seen.append(value.event_id)

    client = _make_client(on_event=on_event)
    client._start_dispatch_consumer()
    client._set_business_session_ready(False)

    await client._queue_events((event,))
    for _ in range(20):
        await asyncio.sleep(0)
    assert seen == []
    assert client._dispatch_inflight is True

    client._set_business_session_ready(True)
    assert client._dispatch_queue is not None
    await client._dispatch_queue.join()

    assert seen == ["retained"]
    await client._stop_dispatch_consumer(drain=True)


@pytest.mark.asyncio
async def test_reconnect_gate_opens_history_send_and_callback_visibility_atomically() -> None:
    event = _event("atomic-ready")
    observed: list[tuple[bool, bool, bool]] = []
    send_results: list[bool] = []
    socket = _SendSocket()
    router = request_router.RequestRouter()
    client: ws_client_module.WsClient

    async def on_event(value: EventEnvelope) -> None:
        assert value is event
        observed.append(
            (
                client._business_session_ready.is_set(),
                client._business_dispatch_ready,
                client._outbound_ready,
            )
        )
        active_socket, active_router = client._require_protocol_context()
        assert active_socket is socket
        assert active_router is router
        send_results.append(await client.send_text("after-ready"))

    client = _make_client(on_event=on_event)
    client._start_dispatch_consumer()
    client._socket = socket
    client._router = router
    client._set_business_session_ready(False)
    await client._queue_events((event,))

    for _ in range(20):
        await asyncio.sleep(0)
    assert observed == []

    client._set_business_session_ready(True)
    assert client._dispatch_queue is not None
    await client._dispatch_queue.join()

    assert observed == [(True, True, True)]
    assert send_results == [True]
    assert socket.sent == ["after-ready"]
    await client._stop_dispatch_consumer(drain=True)


@pytest.mark.asyncio
async def test_stop_deadline_retains_slow_acknowledged_callback_without_waiting_forever(
    monkeypatch,
) -> None:
    event = _event("slow")
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    cancellation_seen = False

    async def on_event(value: EventEnvelope) -> None:
        nonlocal cancellation_seen
        assert value is event
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_seen = True
            await release.wait()
        completed.set()

    client = _make_client(on_event=on_event, stop_timeout_s=0.01)
    monkeypatch.setattr(client, "_emit_state", _noop_emit_state)
    client._start_dispatch_consumer()
    await client._queue_events((event,))
    await started.wait()

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await client.stop()
    elapsed = loop.time() - started_at

    assert elapsed < 0.2
    assert cancellation_seen is False
    assert completed.is_set() is False
    assert client._dispatch_stop_when_drained is True
    assert client._dispatch_task is not None
    assert client._dispatch_task.done() is False

    # The deadline does not discard the already-ACKed work: it finishes later and
    # the lifecycle consumer self-terminates once its retained queue is drained.
    release.set()
    await completed.wait()
    await _wait_until(lambda: client._dispatch_task is None)
    assert client._dispatch_queue is None


@pytest.mark.asyncio
async def test_explicit_stop_flushes_registration_deferred_acknowledged_event(monkeypatch) -> None:
    event = _event("deferred-stop")
    seen: list[str] = []

    async def on_event(value: EventEnvelope) -> None:
        seen.append(value.event_id)

    client = _make_client(on_event=on_event, stop_timeout_s=0.2)
    monkeypatch.setattr(client, "_emit_state", _noop_emit_state)
    client._start_dispatch_consumer()
    client._set_business_session_ready(False)
    await client._deferred_business_slots.acquire()
    client._deferred_business_frames.append((event,))

    await client.stop()

    assert seen == ["deferred-stop"]
    assert len(client._deferred_business_frames) == 0
    assert client._dispatch_task is None
    assert client._dispatch_queue is None


@pytest.mark.asyncio
async def test_exhausted_stop_deadline_hands_deferred_acked_event_to_lifecycle_owner(
    monkeypatch,
) -> None:
    event = _event("deadline-owned")
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def on_event(value: EventEnvelope) -> None:
        assert value is event
        started.set()
        await release.wait()
        completed.set()

    client = _make_client(on_event=on_event, stop_timeout_s=0.0)
    monkeypatch.setattr(client, "_emit_state", _noop_emit_state)
    client._start_dispatch_consumer()
    client._set_business_session_ready(False)
    await client._deferred_business_slots.acquire()
    client._deferred_business_frames.append((event,))

    await client.stop()
    await started.wait()

    assert completed.is_set() is False
    assert len(client._deferred_business_frames) == 0
    assert client._retained_business_drain_task is not None
    assert client._retained_business_drain_task.done() is False
    assert client._dispatch_task is not None
    assert client._dispatch_task.done() is False

    release.set()
    await completed.wait()
    await _wait_until(lambda: client._retained_business_drain_task is None)
    assert client._dispatch_task is None
    assert client._dispatch_queue is None


@pytest.mark.asyncio
async def test_registration_business_backlog_reserves_capacity_before_ack(monkeypatch) -> None:
    monkeypatch.setattr(ws_client_module, "MAX_CALLBACK_QUEUE_SIZE", 1)
    first = _event("business-1")
    second = _event("business-2")
    events_by_mid = {"business-1": first, "business-2": second}

    async def on_event(value: EventEnvelope) -> None:
        return None

    client = _make_client(on_event=on_event)
    client._set_business_session_ready(False)
    monkeypatch.setattr(
        client,
        "_parse_frame_events",
        lambda frame: (events_by_mid[str(frame.headers["mid"])],),
    )
    socket = _ReceiveSocket()
    router = request_router.RequestRouter()
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    socket.push({"code": 200, "headers": {"mid": "business-1"}})
    await _wait_until(lambda: _acked_mids(socket) == ["business-1"])
    socket.push({"code": 200, "headers": {"mid": "business-2"}})
    for _ in range(20):
        await asyncio.sleep(0)

    assert _acked_mids(socket) == ["business-1"]
    assert len(client._deferred_business_frames) == 1

    # Registration completes independently; moving the reserved first item into
    # the dispatcher releases capacity and only then permits ACK of the next push.
    client._set_business_session_ready(True)
    await client._flush_deferred_business_frames()
    await _wait_until(lambda: _acked_mids(socket) == ["business-1", "business-2"])

    await socket.close()
    await receive_task
    assert socket.iterator_count == 1
    assert socket.recv_called is False
    router.close()


@pytest.mark.asyncio
async def test_deferred_sync_extra_reserves_capacity_before_ack(monkeypatch) -> None:
    monkeypatch.setattr(ws_client_module, "MAX_DEFERRED_SYNC_FRAMES", 1)
    started: list[str] = []
    client = _make_client()
    client._set_business_session_ready(False)
    socket = _ReceiveSocket()
    router = request_router.RequestRouter()

    def record_sync_start(ws: Any, active_router: request_router.RequestRouter, frame: WsFrame) -> None:
        assert ws is socket
        assert active_router is router
        started.append(str(frame.headers["mid"]))

    monkeypatch.setattr(client, "_start_sync_exchange", record_sync_start)
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    socket.push(
        {
            "code": 200,
            "headers": {"mid": "sync-1"},
            "body": {"syncExtraType": {"type": 2}},
        }
    )
    await _wait_until(lambda: _acked_mids(socket) == ["sync-1"])
    socket.push(
        {
            "code": 200,
            "headers": {"mid": "sync-2"},
            "body": {"syncExtraType": {"type": 2}},
        }
    )
    for _ in range(20):
        await asyncio.sleep(0)

    assert _acked_mids(socket) == ["sync-1"]
    assert [str(frame.headers["mid"]) for frame in client._deferred_sync_frames] == ["sync-1"]

    client._set_business_session_ready(True)
    client._start_deferred_sync_exchanges(socket, router)
    await _wait_until(lambda: _acked_mids(socket) == ["sync-1", "sync-2"])

    assert started == ["sync-1", "sync-2"]
    assert len(client._deferred_sync_frames) == 0
    await socket.close()
    await receive_task
    assert socket.iterator_count == 1
    assert socket.recv_called is False
    router.close()
