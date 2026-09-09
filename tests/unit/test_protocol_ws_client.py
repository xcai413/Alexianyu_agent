"""Contract tests for canonical WS client orchestration and receive ownership."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from xianyu_agent.protocol import client as legacy_client
from xianyu_agent.protocol.events import ConnectionState, MessageReceived, MessageSent, WsFrame
from xianyu_agent.protocol.ws import (
    client as ws_client_module,
    history as ws_history,
    request_router,
    sync as ws_sync,
)

_CLOSE = object()


class _NoopLock:
    def acquire(self, *, owner_id: str) -> None:
        pass

    def release(self) -> None:
        pass


class _SingleOwnerSocket:
    """Async-iterator socket that fails if orchestration calls recv() directly."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.inbound: asyncio.Queue[str | object] = asyncio.Queue()
        self.iterator_count = 0
        self.recv_called = False
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        self.recv_called = True
        raise AssertionError("canonical orchestration must not create a second recv() owner")

    def __aiter__(self) -> _SingleOwnerSocket:
        self.iterator_count += 1
        if self.iterator_count > 1:
            raise AssertionError("more than one receive iterator owns the socket")
        return self

    async def __anext__(self) -> str:
        item = await self.inbound.get()
        if item is _CLOSE:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.inbound.put_nowait(_CLOSE)

    def push(self, payload: dict[str, Any]) -> None:
        self.inbound.put_nowait(json.dumps(payload))


class _RegistrationBacklogSocket(_SingleOwnerSocket):
    def __init__(self, *, registration_mid: str, backlog_frame: dict[str, Any]) -> None:
        super().__init__()
        self._registration_mid = registration_mid
        self._backlog_frame = backlog_frame
        self._pushed_backlog = False
        self.initial_sync_sent = False

    async def send(self, payload: str) -> None:
        await super().send(payload)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return
        lwp = value.get("lwp")
        if lwp == "/reg" and not self._pushed_backlog:
            self._pushed_backlog = True
            self.push({"code": 200, "headers": {"mid": self._registration_mid}})
            self.push(self._backlog_frame)
            return
        if lwp == "/r/SyncStatus/ackDiff" and value.get("headers", {}).get("mid") == "initial-sync":
            self.initial_sync_sent = True


class _RegistrationBacklogHistorySocket(_RegistrationBacklogSocket):
    async def send(self, payload: str) -> None:
        await super().send(payload)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return
        if value.get("lwp") == ws_history.CONVERSATION_LIST_LWP:
            mid = str(value.get("headers", {}).get("mid"))
            self.push(
                {
                    "code": 200,
                    "headers": {"mid": mid},
                    "body": {"conversations": [{"cid": "cid-from-callback"}]},
                }
            )


class _RegistrationSyncExtraSocket(_SingleOwnerSocket):
    def __init__(self, *, registration_mid: str) -> None:
        super().__init__()
        self._registration_mid = registration_mid
        self._pushed_sync_extra = False
        self.initial_sync_sent = False
        self.get_state_before_initial_sync = False

    async def send(self, payload: str) -> None:
        await super().send(payload)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return
        lwp = value.get("lwp")
        headers = value.get("headers", {})
        mid = str(headers.get("mid", ""))
        if lwp == "/reg" and not self._pushed_sync_extra:
            self._pushed_sync_extra = True
            self.push({"code": 200, "headers": {"mid": self._registration_mid}})
            self.push(
                {
                    "code": 200,
                    "headers": {"mid": "sync-extra-immediate"},
                    "body": {"syncExtraType": {"type": 2}},
                }
            )
            return
        if lwp == ws_sync.ACK_DIFF_LWP and mid == "initial-sync":
            self.initial_sync_sent = True
            return
        if lwp == ws_sync.GET_STATE_LWP:
            self.get_state_before_initial_sync = not self.initial_sync_sent
            self.push(
                {
                    "code": 200,
                    "headers": {"mid": mid},
                    "body": {"pts": 101, "pipeline": "registration"},
                }
            )
            return
        if lwp == ws_sync.ACK_DIFF_LWP and mid != "initial-sync":
            self.push({"code": 200, "headers": {"mid": mid}})
            await self.close()


class _RegistrationBacklogThenErrorSocket(_SingleOwnerSocket):
    def __init__(self, *, registration_mid: str, backlog_frame: dict[str, Any]) -> None:
        super().__init__()
        self._registration_mid = registration_mid
        self._backlog_frame = backlog_frame
        self._pushed_backlog = False

    async def send(self, payload: str) -> None:
        await super().send(payload)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return
        if value.get("lwp") == "/reg" and not self._pushed_backlog:
            self._pushed_backlog = True
            self.push({"code": 200, "headers": {"mid": self._registration_mid}})
            self.push(self._backlog_frame)
            self.inbound.put_nowait(ConnectionError("receive failed before ready"))


class _RegistrationReceiveErrorSocket(_SingleOwnerSocket):
    def __init__(self, *, registration_mid: str) -> None:
        super().__init__()
        self._registration_mid = registration_mid

    async def send(self, payload: str) -> None:
        await super().send(payload)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return
        lwp = value.get("lwp")
        if lwp == "/reg":
            self.push({"code": 200, "headers": {"mid": self._registration_mid}})
            return
        if lwp == "/r/SyncStatus/ackDiff" and value.get("headers", {}).get("mid") == "initial-sync":
            self.inbound.put_nowait(ConnectionError("receive failed"))


class _Credentials:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class _StaticTokenProvider:
    def __init__(self, user_id: str) -> None:
        self._credentials = _Credentials(user_id)

    async def get_credentials(self, account_id: str) -> _Credentials:
        return self._credentials


class _StaticSigner:
    async def load_cookie_value(self, account_id: str) -> str:
        return "cookie-value"


class _SocketContext:
    def __init__(self, socket: _SingleOwnerSocket) -> None:
        self._socket = socket

    async def __aenter__(self) -> _SingleOwnerSocket:
        return self._socket

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._socket.close()


class _FailingSocketContext:
    async def __aenter__(self) -> _SingleOwnerSocket:
        raise ConnectionError("connect failed")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _make_client(*, on_frame=None, timeout: float = 0.2) -> ws_client_module.WsClient:
    return ws_client_module.WsClient(
        "acc-client",
        on_frame=on_frame,
        config=ws_client_module.ClientConfig(
            ws_url="",
            registration_delay_s=0.0,
            registration_timeout_s=timeout,
        ),
        signer=object(),
        token_provider=object(),
        account_lock=_NoopLock(),
    )


def _decoded_sent(socket: _SingleOwnerSocket) -> list[dict[str, Any]]:
    decoded: list[dict[str, Any]] = []
    for payload in socket.sent:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            decoded.append(value)
    return decoded


def _live_backlog_frame(sender_user_id: str) -> dict[str, Any]:
    payload = {
        "1": {
            "2": "cid-backlog@goofish",
            "5": 1_788_912_000_000,
            "10": {
                "reminderContent": "seller backlog",
                "senderUserId": sender_user_id,
                "senderNick": "seller",
            },
        }
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {
        "code": 200,
        "headers": {"mid": "backlog-mid"},
        "body": {"syncPushPackage": {"data": [{"data": encoded}]}},
    }


async def _wait_for_lwp(
    socket: _SingleOwnerSocket,
    lwp: str,
    *,
    occurrence: int = 1,
) -> dict[str, Any]:
    for _ in range(200):
        matches = [frame for frame in _decoded_sent(socket) if frame.get("lwp") == lwp]
        if len(matches) >= occurrence:
            return matches[occurrence - 1]
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for outbound {lwp}")


async def _wait_for_subscription_ready(
    client: ws_client_module.WsClient,
) -> ws_sync.SubscriptionReady:
    for _ in range(200):
        ready = client.subscription_ready
        if ready is not None:
            return ready
        await asyncio.sleep(0)
    raise AssertionError("timed out waiting for SubscriptionReady")


def test_legacy_client_is_a_thin_compatibility_reexport() -> None:
    assert legacy_client.WsClient is ws_client_module.WsClient
    assert legacy_client.ClientConfig is ws_client_module.ClientConfig
    assert legacy_client.ws_request_router is request_router


@pytest.mark.asyncio
async def test_registration_uses_shared_receive_owner_and_preserves_frame_flow(monkeypatch) -> None:
    registration = {"headers": {"mid": "reg-mid"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)

    seen_mids: list[str] = []

    async def on_frame(frame: WsFrame) -> None:
        seen_mids.append(str(frame.headers.get("mid")))

    client = _make_client(on_frame=on_frame)
    socket = _SingleOwnerSocket()
    router = request_router.RequestRouter()
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    registration_task = asyncio.create_task(
        client._register_and_sync(
            socket,
            object(),
            router=router,
            receive_task=receive_task,
        )
    )
    await _wait_for_lwp(socket, "/reg")
    socket.push({"code": 200, "headers": {"mid": "other-mid"}})
    socket.push({"code": 200, "headers": {"mid": "reg-mid"}})
    await registration_task

    sent = _decoded_sent(socket)
    assert sent == [
        registration,
        {"code": 200, "headers": {"mid": "other-mid", "sid": ""}},
        {"code": 200, "headers": {"mid": "reg-mid", "sid": ""}},
        initial_sync,
    ]
    assert router.pending_count == 0
    assert socket.iterator_count == 1
    assert socket.recv_called is False

    await socket.close()
    await receive_task
    assert seen_mids == ["other-mid", "reg-mid"]
    router.close()


@pytest.mark.asyncio
async def test_registration_timeout_does_not_cancel_or_replace_receive_owner(monkeypatch) -> None:
    registration = {"headers": {"mid": "reg-timeout"}, "lwp": "/reg"}
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )

    client = _make_client(timeout=0.01)
    socket = _SingleOwnerSocket()
    router = request_router.RequestRouter()
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    with pytest.raises(TimeoutError, match="IM registration response timeout"):
        await client._register_and_sync(
            socket,
            object(),
            router=router,
            receive_task=receive_task,
        )

    assert router.pending_count == 0
    assert receive_task.done() is False
    assert socket.iterator_count == 1
    assert socket.recv_called is False

    await socket.close()
    await receive_task
    router.close()


@pytest.mark.asyncio
async def test_connect_binds_current_user_before_immediate_registration_backlog(monkeypatch) -> None:
    seller_user_id = "seller-user-42"
    registration = {"headers": {"mid": "reg-backlog"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    socket = _RegistrationBacklogSocket(
        registration_mid="reg-backlog",
        backlog_frame=_live_backlog_frame(seller_user_id),
    )
    events: list[Any] = []

    async def on_event(event: Any) -> None:
        events.append(event)
        if isinstance(event, MessageSent):
            await socket.close()

    client = ws_client_module.WsClient(
        "acc-client",
        on_event=on_event,
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.0,
            registration_timeout_s=0.2,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider(seller_user_id),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    await client._connect_and_serve()

    sent_events = [event for event in events if isinstance(event, MessageSent)]
    received_events = [event for event in events if isinstance(event, MessageReceived)]
    assert len(sent_events) == 1
    assert sent_events[0].content == "seller backlog"
    assert received_events == []
    assert socket.initial_sync_sent is True
    assert socket.iterator_count == 1
    assert socket.recv_called is False
    assert client._account_user_id is None


@pytest.mark.asyncio
async def test_registration_backlog_business_callback_waits_for_outbound_ready(monkeypatch) -> None:
    seller_user_id = "seller-user-42"
    registration = {"headers": {"mid": "reg-buyer-backlog"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    socket = _RegistrationBacklogSocket(
        registration_mid="reg-buyer-backlog",
        backlog_frame=_live_backlog_frame("buyer-user-7"),
    )
    callback_after_initial_sync: list[bool] = []
    send_results: list[bool] = []
    client: ws_client_module.WsClient

    async def on_event(event: Any) -> None:
        if isinstance(event, MessageReceived):
            callback_after_initial_sync.append(socket.initial_sync_sent)
            assert client._socket is socket
            send_results.append(await client.send_text("reply-after-ready"))
            await socket.close()

    client = ws_client_module.WsClient(
        "acc-client",
        on_event=on_event,
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.05,
            registration_timeout_s=0.2,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider(seller_user_id),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    await client._connect_and_serve()

    backlog_ack_index = next(
        index
        for index, payload in enumerate(socket.sent)
        if payload.startswith("{")
        and json.loads(payload).get("headers", {}).get("mid") == "backlog-mid"
    )
    initial_sync_index = socket.sent.index(json.dumps(initial_sync))
    reply_index = socket.sent.index("reply-after-ready")
    assert callback_after_initial_sync == [True]
    assert send_results == [True]
    assert backlog_ack_index < initial_sync_index < reply_index
    assert socket.iterator_count == 1
    assert socket.recv_called is False
    assert client._socket is None
    assert client._outbound_ready is False
    assert len(client._deferred_business_frames) == 0


@pytest.mark.asyncio
async def test_business_callback_can_await_history_without_blocking_receive_owner(monkeypatch) -> None:
    seller_user_id = "seller-user-42"
    registration = {"headers": {"mid": "reg-history-callback"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    socket = _RegistrationBacklogHistorySocket(
        registration_mid="reg-history-callback",
        backlog_frame=_live_backlog_frame("buyer-user-7"),
    )
    history_payloads: list[Any] = []
    client: ws_client_module.WsClient

    async def on_event(event: Any) -> None:
        if isinstance(event, MessageReceived):
            response = await client.request_conversations(timeout_s=0.2)
            history_payloads.append(response.payload["body"])
            await socket.close()

    client = ws_client_module.WsClient(
        "acc-client",
        on_event=on_event,
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.0,
            registration_timeout_s=0.2,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider(seller_user_id),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    await client._connect_and_serve()

    assert history_payloads == [{"conversations": [{"cid": "cid-from-callback"}]}]
    assert socket.iterator_count == 1
    assert socket.recv_called is False
    assert client._dispatch_task is None
    assert client._dispatch_queue is None


@pytest.mark.asyncio
async def test_callback_queue_backpressures_before_ack(monkeypatch) -> None:
    release = asyncio.Event()
    first_started = asyncio.Event()

    async def on_frame(frame: WsFrame) -> None:
        if frame.headers.get("mid") == "callback-1":
            first_started.set()
            await release.wait()

    monkeypatch.setattr(ws_client_module, "MAX_CALLBACK_QUEUE_SIZE", 1)
    client = _make_client(on_frame=on_frame)
    socket = _SingleOwnerSocket()
    router = request_router.RequestRouter()
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    socket.push({"code": 200, "headers": {"mid": "callback-1"}})
    await first_started.wait()
    socket.push({"code": 200, "headers": {"mid": "callback-2"}})
    for _ in range(20):
        await asyncio.sleep(0)
    socket.push({"code": 200, "headers": {"mid": "callback-3"}})
    for _ in range(20):
        await asyncio.sleep(0)

    acked_before_release = [
        str(frame.get("headers", {}).get("mid"))
        for frame in _decoded_sent(socket)
        if frame.get("code") == 200
    ]
    assert acked_before_release == ["callback-1", "callback-2"]

    release.set()
    for _ in range(200):
        acked = [
            str(frame.get("headers", {}).get("mid"))
            for frame in _decoded_sent(socket)
            if frame.get("code") == 200
        ]
        if "callback-3" in acked:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("third frame was not ACKed after callback capacity became available")

    await socket.close()
    await receive_task
    assert socket.iterator_count == 1
    assert socket.recv_called is False
    router.close()


@pytest.mark.asyncio
async def test_connection_teardown_keeps_acknowledged_callback_consumer_alive(monkeypatch) -> None:
    registration = {"headers": {"mid": "reg-retain-callback"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    socket = _RegistrationBacklogSocket(
        registration_mid="reg-retain-callback",
        backlog_frame=_live_backlog_frame("buyer-user-7"),
    )
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_completed = asyncio.Event()

    async def on_event(event: Any) -> None:
        if isinstance(event, MessageReceived):
            callback_started.set()
            await release_callback.wait()
            callback_completed.set()

    async def on_state(event) -> None:
        if event.state == ConnectionState.CONNECTED:
            await callback_started.wait()
            await socket.close()

    client = ws_client_module.WsClient(
        "acc-client",
        on_event=on_event,
        on_state=on_state,
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.0,
            registration_timeout_s=0.2,
            stop_timeout_s=0.01,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider("seller-user-42"),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    client._start_dispatch_consumer()
    await client._connect_and_serve()

    assert callback_started.is_set()
    assert callback_completed.is_set() is False
    assert client._dispatch_task is not None
    assert client._dispatch_task.done() is False
    assert client._dispatch_queue is not None
    assert client._socket is None
    assert client._outbound_ready is False

    release_callback.set()
    await client._dispatch_queue.join()
    assert callback_completed.is_set()
    await client._stop_dispatch_consumer(drain=True)


@pytest.mark.asyncio
async def test_send_text_rejects_transport_until_outbound_ready() -> None:
    client = _make_client()
    socket = _SingleOwnerSocket()
    client._socket = socket
    client._outbound_ready = False

    assert await client.send_text("too-early") is False
    assert socket.sent == []

    client._outbound_ready = True
    assert await client.send_text("ready-text") is True
    assert socket.sent == ["ready-text"]

    client._outbound_ready = False
    client._socket = None


@pytest.mark.asyncio
async def test_registration_sync_extra_waits_for_initial_ack_diff(monkeypatch) -> None:
    registration = {"headers": {"mid": "reg-sync-extra"}, "lwp": "/reg"}
    initial_sync = {"lwp": ws_sync.ACK_DIFF_LWP, "headers": {"mid": "initial-sync"}}
    socket = _RegistrationSyncExtraSocket(registration_mid="reg-sync-extra")
    client = ws_client_module.WsClient(
        "acc-client",
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.05,
            registration_timeout_s=0.2,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider("seller-user-42"),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    await client._connect_and_serve()

    decoded = _decoded_sent(socket)
    initial_sync_index = next(
        index
        for index, frame in enumerate(decoded)
        if frame.get("lwp") == ws_sync.ACK_DIFF_LWP
        and frame.get("headers", {}).get("mid") == "initial-sync"
    )
    get_state_index = next(
        index for index, frame in enumerate(decoded) if frame.get("lwp") == ws_sync.GET_STATE_LWP
    )
    assert socket.initial_sync_sent is True
    assert socket.get_state_before_initial_sync is False
    assert initial_sync_index < get_state_index
    assert client._deferred_sync_frames == client._deferred_sync_frames.__class__()
    assert socket.iterator_count == 1
    assert socket.recv_called is False


@pytest.mark.asyncio
async def test_registration_backlog_is_retained_when_receive_fails_before_ready(monkeypatch) -> None:
    seller_user_id = "seller-user-42"
    registration = {"headers": {"mid": "reg-backlog-error"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    socket = _RegistrationBacklogThenErrorSocket(
        registration_mid="reg-backlog-error",
        backlog_frame=_live_backlog_frame("buyer-user-7"),
    )
    events: list[Any] = []
    seen_mids: list[str] = []

    async def on_event(event: Any) -> None:
        events.append(event)

    async def on_frame(frame: WsFrame) -> None:
        seen_mids.append(str(frame.headers.get("mid")))

    client = ws_client_module.WsClient(
        "acc-client",
        on_event=on_event,
        on_frame=on_frame,
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.01,
            registration_timeout_s=0.2,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider(seller_user_id),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    with pytest.raises(ConnectionError, match="receive failed before ready"):
        await client._connect_and_serve()

    assert events == []
    assert "backlog-mid" in seen_mids
    assert len(client._deferred_business_frames) == 1
    assert len(client._deferred_sync_frames) == 0
    assert client._business_dispatch_ready is False
    assert client._outbound_ready is False
    assert client._receive_task is None
    assert client._router is None
    assert client._socket is None
    assert client._account_user_id is None
    assert client._dispatch_task is None
    assert client._dispatch_queue is None

    client._start_dispatch_consumer()
    await client._flush_deferred_business_frames()
    assert client._dispatch_queue is not None
    await client._dispatch_queue.join()
    assert len(events) == 1
    assert isinstance(events[0], MessageReceived)
    assert len(client._deferred_business_frames) == 0
    await client._stop_dispatch_consumer(drain=True)


@pytest.mark.asyncio
async def test_receive_error_still_clears_connection_protocol_context(monkeypatch) -> None:
    seller_user_id = "seller-user-42"
    registration = {"headers": {"mid": "reg-receive-error"}, "lwp": "/reg"}
    initial_sync = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "initial-sync"}}
    socket = _RegistrationReceiveErrorSocket(registration_mid="reg-receive-error")
    client = ws_client_module.WsClient(
        "acc-client",
        config=ws_client_module.ClientConfig(
            ws_url="wss://unit.test/ws",
            registration_delay_s=0.0,
            registration_timeout_s=0.2,
        ),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider(seller_user_id),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module,
        "build_registration_frame",
        lambda _credentials: registration,
    )
    monkeypatch.setattr(ws_client_module, "build_sync_frame", lambda: initial_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    with pytest.raises(ConnectionError, match="receive failed"):
        await client._connect_and_serve()

    assert client._receive_task is None
    assert client._router is None
    assert client._socket is None
    assert client._account_user_id is None
    assert client._outbound_ready is False
    assert client._dispatch_task is None
    assert client._dispatch_queue is None


@pytest.mark.asyncio
async def test_connection_teardown_clears_subscription_ready(monkeypatch) -> None:
    socket = _SingleOwnerSocket()
    ready = ws_sync.SubscriptionReady(sync_type=2, state_body={"pts": 9})
    client: ws_client_module.WsClient

    async def on_state(event) -> None:
        if event.state == ConnectionState.CONNECTED:
            await socket.close()

    client = ws_client_module.WsClient(
        "acc-client",
        on_state=on_state,
        config=ws_client_module.ClientConfig(ws_url="wss://unit.test/ws"),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider("seller-user-42"),
        account_lock=_NoopLock(),
    )

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    async def fake_register_and_sync(*args, **kwargs) -> None:
        client._subscription_ready = ready

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(client, "_register_and_sync", fake_register_and_sync)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _SocketContext(socket),
    )

    await client._connect_and_serve()

    assert client.subscription_ready is None
    assert client._socket is None
    assert client._router is None
    assert client._outbound_ready is False


@pytest.mark.asyncio
async def test_connect_failure_clears_stale_account_user_id(monkeypatch) -> None:
    client = ws_client_module.WsClient(
        "acc-client",
        config=ws_client_module.ClientConfig(ws_url="wss://unit.test/ws"),
        signer=_StaticSigner(),
        token_provider=_StaticTokenProvider("current-user"),
        account_lock=_NoopLock(),
    )
    client._account_user_id = "stale-user"
    client._subscription_ready = ws_sync.SubscriptionReady(sync_type=2, state_body={})
    client._outbound_ready = True

    async def skip_status_update(*, state: ConnectionState, detail: str | None) -> None:
        return None

    monkeypatch.setattr(client, "_update_worker_status", skip_status_update)
    monkeypatch.setattr(
        ws_client_module.ws_connector,
        "open_connection",
        lambda _url, _cookie: _FailingSocketContext(),
    )

    with pytest.raises(ConnectionError, match="connect failed"):
        await client._connect_and_serve()

    assert client._account_user_id is None
    assert client.subscription_ready is None
    assert len(client._deferred_business_frames) == 0
    assert len(client._deferred_sync_frames) == 0
    assert client._business_dispatch_ready is False
    assert client._outbound_ready is False


@pytest.mark.asyncio
async def test_sync_extra_uses_same_router_and_produces_protocol_ready_only() -> None:
    client = _make_client()
    socket = _SingleOwnerSocket()
    router = request_router.RequestRouter()
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    socket.push(
        {
            "code": 200,
            "headers": {"mid": "sync-push"},
            "body": {"syncExtraType": {"type": 2}},
        }
    )

    get_state = await _wait_for_lwp(socket, ws_sync.GET_STATE_LWP)
    get_mid = str(get_state["headers"]["mid"])
    state_body = {"pipeline": "sync", "pts": 77, "nested": {"value": 1}}
    socket.push({"code": 200, "headers": {"mid": get_mid}, "body": state_body})

    ack_diff = await _wait_for_lwp(socket, ws_sync.ACK_DIFF_LWP)
    assert ack_diff["body"] == [state_body]
    ack_mid = str(ack_diff["headers"]["mid"])
    socket.push({"code": 200, "headers": {"mid": ack_mid}})

    ready = await _wait_for_subscription_ready(client)
    assert ready == ws_sync.SubscriptionReady(sync_type=2, state_body=state_body)
    assert client.state == ConnectionState.IDLE
    assert router.pending_count == 0
    assert socket.iterator_count == 1
    assert socket.recv_called is False

    await socket.close()
    await receive_task
    await client._cancel_protocol_tasks()
    router.close()


@pytest.mark.asyncio
async def test_history_rejects_requests_until_registration_ready() -> None:
    client = _make_client()
    socket = _SingleOwnerSocket()
    router = request_router.RequestRouter()
    client._socket = socket
    client._router = router
    client._business_dispatch_ready = False

    with pytest.raises(ConnectionError, match="protocol session is not connected"):
        await client.request_conversations(timeout_s=0.01)

    assert socket.sent == []
    assert router.pending_count == 0

    client._socket = None
    client._router = None
    client._business_dispatch_ready = True
    router.close()


@pytest.mark.asyncio
async def test_history_requests_share_receive_owner_and_router() -> None:
    client = _make_client()
    socket = _SingleOwnerSocket()
    router = request_router.RequestRouter()
    client._socket = socket
    client._router = router
    receive_task = asyncio.create_task(client._receive_loop(socket, router))

    conversations_task = asyncio.create_task(client.request_conversations(timeout_s=0.2))
    conversations_request = await _wait_for_lwp(socket, ws_history.CONVERSATION_LIST_LWP)
    conversations_mid = str(conversations_request["headers"]["mid"])
    socket.push(
        {
            "code": 200,
            "headers": {"mid": conversations_mid},
            "body": {"conversations": []},
        }
    )
    conversations = await conversations_task
    assert conversations.payload["body"] == {"conversations": []}

    history_task = asyncio.create_task(
        client.request_message_history("cid-1", timeout_s=0.2)
    )
    history_request = await _wait_for_lwp(socket, ws_history.MESSAGE_HISTORY_LWP)
    history_mid = str(history_request["headers"]["mid"])
    socket.push(
        {
            "code": 200,
            "headers": {"mid": history_mid},
            "body": {"messages": []},
        }
    )
    history = await history_task
    assert history.payload["body"] == {"messages": []}

    assert router.pending_count == 0
    assert socket.iterator_count == 1
    assert socket.recv_called is False

    client._socket = None
    client._router = None
    await socket.close()
    await receive_task
    router.close()


@pytest.mark.asyncio
async def test_history_requires_active_protocol_session() -> None:
    client = _make_client()

    with pytest.raises(ConnectionError, match="protocol session is not connected"):
        await client.request_conversations()
