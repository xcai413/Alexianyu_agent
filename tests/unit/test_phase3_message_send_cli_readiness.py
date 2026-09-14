from __future__ import annotations

import asyncio

import pytest
import typer

from xianyu_agent.cli.commands import message as message_cli
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.runtime.account_lock import AccountConnectionAlreadyRunningError


class _FakeClient:
    def __init__(self, state: ConnectionState = ConnectionState.IDLE) -> None:
        self.state = state


class _FakeWorker:
    def __init__(self, state: ConnectionState = ConnectionState.IDLE) -> None:
        self._client = _FakeClient(state)


@pytest.mark.asyncio
async def test_wait_for_protocol_ready_blocks_until_connected() -> None:
    worker = _FakeWorker()
    waiter = asyncio.create_task(
        message_cli._wait_for_protocol_ready(worker, timeout_s=0.5)  # type: ignore[arg-type]
    )

    await asyncio.sleep(0)
    assert waiter.done() is False

    worker._client.state = ConnectionState.CONNECTED
    await waiter


@pytest.mark.asyncio
async def test_wait_for_protocol_ready_times_out_before_any_send() -> None:
    worker = _FakeWorker()

    with pytest.raises(TimeoutError, match="readiness timed out"):
        await message_cli._wait_for_protocol_ready(  # type: ignore[arg-type]
            worker,
            timeout_s=0.0,
        )


def test_message_send_reports_existing_daemon_worker_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Store:
        async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str:
            assert account_id == "acc-1"
            assert chat_id == "chat-1"
            return "buyer-1"

    class _LockedWorker:
        def __init__(self, account_id: str) -> None:
            assert account_id == "acc-1"

        def start(self) -> None:
            raise AccountConnectionAlreadyRunningError("账号 WS 连接锁已被占用")

    monkeypatch.setattr(message_cli, "DomainMessageStore", _Store)
    monkeypatch.setattr(message_cli, "AccountWorker", _LockedWorker)

    with pytest.raises(typer.Exit) as caught:
        message_cli.send_message(
            account_id="acc-1",
            chat_id="chat-1",
            text="hello",
            receiver_id="",
        )

    assert caught.value.exit_code == 2
    output = capsys.readouterr().out
    assert "已有常驻 WebSocket 连接" in output
    assert "pool stop --account acc-1" in output
