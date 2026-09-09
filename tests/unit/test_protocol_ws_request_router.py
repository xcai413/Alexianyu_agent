"""Isolation and regression tests for the M5 WebSocket request router split."""

from __future__ import annotations

import asyncio

import pytest

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import request_router


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
