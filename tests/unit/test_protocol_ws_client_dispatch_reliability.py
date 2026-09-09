"""Targeted regressions for WS callback lifecycle reliability."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from xianyu_agent.protocol.events import EventEnvelope
from xianyu_agent.protocol.ws import client as ws_client_module, request_router


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
    client._outbound_ready = True
    client._business_dispatch_ready = True
    client._business_session_ready.set()

    await client._queue_events((first, second))
    await _wait_until(lambda: router.pending_count == 1)

    # Reproduce connection teardown ordering: pause future business callbacks first,
    # then close the router so the first event's history waiter is cancelled.
    client._business_session_ready.clear()
    client._business_dispatch_ready = False
    client._outbound_ready = False
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
    client._outbound_ready = True
    client._business_dispatch_ready = True
    client._business_session_ready.set()

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
    client._business_session_ready.clear()
    client._business_dispatch_ready = False
    client._outbound_ready = False

    await client._queue_events((event,))
    for _ in range(20):
        await asyncio.sleep(0)
    assert seen == []
    assert client._dispatch_inflight is True

    client._outbound_ready = True
    client._business_dispatch_ready = True
    client._business_session_ready.set()
    assert client._dispatch_queue is not None
    await client._dispatch_queue.join()

    assert seen == ["retained"]
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
    client._business_session_ready.clear()
    client._business_dispatch_ready = False
    client._outbound_ready = False
    client._deferred_business_frames.append((event,))

    await client.stop()

    assert seen == ["deferred-stop"]
    assert len(client._deferred_business_frames) == 0
    assert client._dispatch_task is None
    assert client._dispatch_queue is None
