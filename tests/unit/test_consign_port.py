"""The runtime platform-confirmation port classifies platform outcomes."""

from __future__ import annotations

import pytest

from xianyu_agent.domain.delivery import FailureKind
from xianyu_agent.protocol.mtop.endpoints.consign import (
    ConsignError,
    ConsignRejectedError,
    ConsignResult,
    ConsignUncertainError,
)
from xianyu_agent.runtime.delivery_sender import make_consign_port


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.seen: list[tuple[str, str, str]] = []

    async def confirm_delivery(
        self, account_id: str, order_id: str, *, trade_text: str = "", pic_list=()
    ) -> ConsignResult:
        self.seen.append((account_id, order_id, trade_text))
        if self._error is not None:
            raise self._error
        return ConsignResult(order_id=order_id, confirmed=True)


@pytest.mark.asyncio
async def test_consign_port_maps_success() -> None:
    client = _Client()
    outcome = await make_consign_port(client)("acc-1", "O-1", "CARD-1")  # type: ignore[arg-type]

    assert outcome.confirmed is True
    assert client.seen == [("acc-1", "O-1", "CARD-1")]


@pytest.mark.asyncio
async def test_consign_port_maps_business_rejection_to_rejected() -> None:
    client = _Client(ConsignRejectedError("确认发货被拒绝: 订单状态错误"))

    outcome = await make_consign_port(client)("acc-1", "O-1", "")  # type: ignore[arg-type]

    assert outcome.confirmed is False
    assert outcome.failure is FailureKind.REJECTED
    assert "订单状态错误" in (outcome.error or "")


@pytest.mark.asyncio
async def test_consign_port_maps_transport_failure_and_unexpected_error_to_unknown() -> None:
    transport = _Client(ConsignUncertainError("确认发货服务异常,结果不明(HTTP 503)"))
    mapped = await make_consign_port(transport)("acc-1", "O-1", "")  # type: ignore[arg-type]
    assert mapped.failure is FailureKind.UNKNOWN

    missing_credential = _Client(ConsignError("账号 acc-1 无可用 Cookie"))
    mapped = await make_consign_port(missing_credential)(  # type: ignore[arg-type]
        "acc-1", "O-1", ""
    )
    assert mapped.failure is FailureKind.UNKNOWN

    class _Exploding:
        async def confirm_delivery(self, *_args, **_kwargs):
            raise ValueError("unexpected")

    mapped = await make_consign_port(_Exploding())("acc-1", "O-1", "")  # type: ignore[arg-type]
    assert mapped.failure is FailureKind.UNKNOWN
    assert "ValueError" in (mapped.error or "")
