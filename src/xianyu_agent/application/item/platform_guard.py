"""真实平台调用前的风控闸:先判风控状态,再按账号节流。

闲鱼在判定请求异常时返回 FAIL_SYS_USER_VALIDATE(伴随 RGV587 之类的风控提示)。项目对
这个信号的处理是:停止该账号的自动调用并进入冷却,等人工在 App 里处理,不做任何绕过。

worker 路径早已接入这套机制,但命令行与 REST 的商品命令此前没有:运营方(或 Agent)可以
在冷却期内反复重试,把账号推得更深。本闸把同样的规则放到这些入口前面——冷却期内直接
拒绝发请求,命中人工验证时打开熔断,冷却结束后仍需要一次成功的人工操作才能恢复。

风控检查通过后再取一次该账号的节流令牌(见 application/pacing.py),让批量铺货、同步这类
短时间多次请求的流程不形成突发节奏。顺序是固定的:被风控挡下的调用不消耗令牌,因为那时
根本不该有平台请求。

已知边界:节流按应用层的一次操作计一次令牌,分页循环(同一客户端内的多次请求)不单独计数,
真实突发来源是批量铺货的逐行发布,那一层是按行节流的。
"""

from __future__ import annotations

import math

from xianyu_agent.application.pacing import AccountPacer, get_pacer
from xianyu_agent.domain.account import risk as account_risk


class AccountCoolingError(RuntimeError):
    """账号处于人工验证冷却期,当前不应发起平台请求。"""


class AccountRiskGate:
    """Check the account risk circuit and pace requests before platform calls."""

    def __init__(self, *, pacer: AccountPacer | None = None) -> None:
        self._pacer = pacer if pacer is not None else get_pacer()

    async def ensure_ready(self, account_id: str) -> None:
        """Refuse platform calls while the account still needs human recovery.

        The project's rule is that the cooldown alone does not resume automation:
        after it expires the account still needs one successful manual refresh, so
        both states block a call here. A call that passes takes one pacing token
        for the account.
        """
        circuit = await account_risk.get(account_id)
        if circuit is not None:
            if not circuit.is_cooling():
                msg = (
                    f"账号 {account_id} 的人工验证熔断尚未解除,"
                    "请先在闲鱼 App 完成验证并执行一次 auth refresh"
                )
                raise AccountCoolingError(msg)
            remaining = math.ceil(circuit.remaining_seconds() / 60)
            msg = (
                f"账号 {account_id} 处于人工验证冷却(约剩 {remaining} 分钟),"
                "请先在闲鱼 App 完成验证,冷却结束后再试"
            )
            raise AccountCoolingError(msg)
        await self._pacer.acquire(account_id)

    async def record_failure(self, account_id: str, error: BaseException) -> bool:
        """Open the circuit when the platform asked for human verification."""
        if not account_risk.is_user_validate_error(error):
            return False
        await account_risk.open_user_validate(
            account_id, code=account_risk.detected_risk_code(error)
        )
        return True
