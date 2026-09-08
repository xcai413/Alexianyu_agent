"""Isolation and regression tests for the M5 WebSocket request router split."""

from __future__ import annotations

import asyncio
import json

import pytest

from xianyu_agent.protocol import client
from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import request_router


class _NoopLock:
    def acquire(self, *, owner_id: str) -> None:
        pass

    def release(self) -> None:
        pass


class _RegistrationSocket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if not self.frames:
            msg = "unexpected extra recv"
            raise AssertionError(msg)
        return self.frames.pop(0)


class _BlockingSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.recv_cancelled = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        event = asyncio.Event()
        try:
            await event.wait()
        except asyncio.CancelledError:
            self.recv_cancelled = True
            raise
        msg = "unreachable"
        raise AssertionError(msg)


def _make_client(*, on_frame=None, timeout: float = 0.2) -> client.WsClient:
    return client.WsClient(
        "acc-router",
        on_frame=on_frame,
        config=client.ClientConfig(
            ws_url="",
            registration_delay_s=0.0,
            registration_timeout_s=timeout,
        ),
        signer=object(),
        token_provider=object(),
        account_lock=_NoopLock(),
    )


@pytest.mark.asyncio
async def test_register_match_and_wait_contract() -> None:
    router = request_router.RequestRouter()
    pending = router.register("m-1")

    assert router.pending_count == 1
    assert router.match("m-1", {"ok": True}) is True
    assert router.pending_count == 0
    assert await router.wait(pending, timeout_s=0.1) == {"ok": True}


@pytest.mark.asyncio
async def test_concurrent_requests_are_isolated_when_responses_arrive_out_of_order() -> None:
    router = request_router.RequestRouter()
    first = router.register("m-1")
    second = router.register("m-2")

    first_waiter = asyncio.create_task(router.wait(first, timeout_s=0.2))
    second_waiter = asyncio.create_task(router.wait(second, timeout_s=0.2))
    await asyncio.sleep(0)

    assert router.match("m-2", "second") is True
    assert router.match("m-1", "first") is True

    assert await asyncio.gather(first_waiter, second_waiter) == ["first", "second"]
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_timeout_cleans_pending_state_and_cancels_future() -> None:
    router = request_router.RequestRouter()
    pending = router.register("m-timeout")

    with pytest.raises(TimeoutError):
        await router.wait(pending, timeout_s=0.01)

    assert router.pending_count == 0
    assert pending.future.cancelled()


@pytest.mark.asyncio
async def test_waiter_cancellation_cleans_pending_state() -> None:
    router = request_router.RequestRouter()
    pending = router.register("m-cancel")
    waiter = asyncio.create_task(router.wait(pending, timeout_s=None))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert router.pending_count == 0
    assert pending.future.cancelled()


@pytest.mark.asyncio
async def test_failed_request_propagates_error_and_cleans_pending_state() -> None:
    router = request_router.RequestRouter()
    pending = router.register("m-error")

    assert router.fail("m-error", RuntimeError("recv failed")) is True
    with pytest.raises(RuntimeError, match="recv failed"):
        await router.wait(pending, timeout_s=0.1)

    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_unknown_missing_and_late_responses_are_safe() -> None:
    router = request_router.RequestRouter()
    pending = router.register("m-known")

    assert router.match("m-unknown", object()) is False
    assert router.match_frame(WsFrame(headers={}), object()) is False
    assert router.match_frame(WsFrame(headers={"mid": "m-known"}), "matched") is True
    assert router.match("m-known", "late-duplicate") is False
    assert pending.future.result() == "matched"
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_duplicate_registration_is_rejected() -> None:
    router = request_router.RequestRouter()
    router.register("m-duplicate")

    with pytest.raises(ValueError, match="already pending"):
        router.register("m-duplicate")
    router.close()


@pytest.mark.asyncio
async def test_close_cancels_all_pending_and_prevents_new_registration() -> None:
    router = request_router.RequestRouter()
    first = router.register("m-1")
    second = router.register("m-2")

    router.close()

    assert router.closed is True
    assert router.pending_count == 0
    assert first.future.cancelled()
    assert second.future.cancelled()
    with pytest.raises(RuntimeError, match="closed"):
        router.register("m-3")


def test_request_id_from_frame_normalizes_mid_and_handles_missing_headers() -> None:
    assert request_router.request_id_from_frame(WsFrame(headers={"mid": 123})) == "123"
    assert request_router.request_id_from_frame(WsFrame(headers={"mid": ""})) is None
    assert request_router.request_id_from_frame(WsFrame(headers={})) is None


@pytest.mark.asyncio
async def test_ws_client_registration_uses_router_without_changing_frame_flow(monkeypatch) -> None:
    registration = {"headers": {"mid": "reg-mid"}, "lwp": "/reg"}
    sync = {"lwp": "/r/SyncStatus/ackDiff"}
    monkeypatch.setattr(client, "build_registration_frame", lambda _credentials: registration)
    monkeypatch.setattr(client, "build_sync_frame", lambda: sync)

    seen_mids: list[str] = []

    async def on_frame(frame: WsFrame) -> None:
        seen_mids.append(str(frame.headers.get("mid")))

    ws_client = _make_client(on_frame=on_frame)
    socket = _RegistrationSocket(
        [
            json.dumps({"code": 200, "headers": {"mid": "other-mid"}}),
            json.dumps({"code": 200, "headers": {"mid": "reg-mid"}}),
        ]
    )

    await ws_client._register_and_sync(socket, object())

    sent = [json.loads(payload) for payload in socket.sent]
    assert sent == [
        registration,
        {"code": 200, "headers": {"mid": "other-mid", "sid": ""}},
        {"code": 200, "headers": {"mid": "reg-mid", "sid": ""}},
        sync,
    ]
    assert seen_mids == ["other-mid", "reg-mid"]


@pytest.mark.asyncio
async def test_ws_client_registration_timeout_cancels_receive_task(monkeypatch) -> None:
    registration = {"headers": {"mid": "reg-timeout"}, "lwp": "/reg"}
    monkeypatch.setattr(client, "build_registration_frame", lambda _credentials: registration)

    ws_client = _make_client(timeout=0.01)
    socket = _BlockingSocket()

    with pytest.raises(TimeoutError, match="IM registration response timeout"):
        await ws_client._register_and_sync(socket, object())

    assert socket.recv_cancelled is True


def test_ws_client_uses_canonical_request_router_module() -> None:
    assert client.ws_request_router is request_router
