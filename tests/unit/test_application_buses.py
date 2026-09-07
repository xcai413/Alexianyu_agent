from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from xianyu_agent.application.ports.outbox import OutboxRecord
from xianyu_agent.infrastructure.command_bus import (
    DuplicateHandlerError,
    HandlerNotRegisteredError,
    InProcessCommandBus,
)
from xianyu_agent.infrastructure.event_bus import InProcessEventBus


@dataclass(frozen=True)
class ExampleCommand:
    value: int


@pytest.mark.asyncio
async def test_command_bus_dispatches_exact_type_and_returns_result() -> None:
    bus = InProcessCommandBus()

    async def handle(command: object) -> object:
        assert isinstance(command, ExampleCommand)
        return command.value + 1

    bus.register(ExampleCommand, handle)

    assert await bus.dispatch(ExampleCommand(41)) == 42


@pytest.mark.asyncio
async def test_command_bus_rejects_missing_and_duplicate_handlers() -> None:
    bus = InProcessCommandBus()

    async def handle(command: object) -> object:
        return command

    bus.register(ExampleCommand, handle)
    with pytest.raises(DuplicateHandlerError):
        bus.register(ExampleCommand, handle)
    with pytest.raises(HandlerNotRegisteredError):
        await bus.dispatch(object())


def _event(event_type: str = "OrderPaid") -> OutboxRecord:
    return OutboxRecord(
        id=1,
        event_id="evt-1",
        event_type=event_type,
        version=1,
        account_id="acc-1",
        aggregate_type="order",
        aggregate_id="order-1",
        correlation_id="corr-1",
        causation_id=None,
        payload={"order_id": "order-1"},
        occurred_at=datetime.now(UTC),
        published_at=None,
        attempt_count=0,
        last_error=None,
    )


@pytest.mark.asyncio
async def test_event_bus_fans_out_in_registration_order() -> None:
    bus = InProcessEventBus()
    seen: list[str] = []

    async def first(event: OutboxRecord) -> None:
        seen.append(f"first:{event.event_id}")

    async def second(event: OutboxRecord) -> None:
        seen.append(f"second:{event.event_id}")

    bus.subscribe("OrderPaid", first)
    bus.subscribe("OrderPaid", first)
    bus.subscribe("OrderPaid", second)

    await bus.publish(_event())

    assert seen == ["first:evt-1", "second:evt-1"]


@pytest.mark.asyncio
async def test_event_bus_propagates_handler_failure() -> None:
    bus = InProcessEventBus()

    async def fail(_event: OutboxRecord) -> None:
        raise RuntimeError("consumer failed")

    bus.subscribe("OrderPaid", fail)

    with pytest.raises(RuntimeError, match="consumer failed"):
        await bus.publish(_event())
