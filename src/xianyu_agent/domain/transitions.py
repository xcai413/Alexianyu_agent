"""通用的状态流转判定。

状态机散落在各领域里,但"这次转换允不允许"的判断是同一件事,所以放在这里共用:
库存单元、预留、发货任务都引用它,避免每个模块各写一份比较逻辑。
"""

from __future__ import annotations

from collections.abc import Mapping


class InvalidStateTransition(RuntimeError):
    """状态流转被拒绝:调用方试图跳过一次不允许的转换。"""


def can_transition(table: Mapping[str, frozenset[str]], current: str, target: str) -> bool:
    """Return whether current -> target is allowed by the transition table."""
    return target in table.get(current, frozenset())


def require_transition(
    table: Mapping[str, frozenset[str]],
    current: str,
    target: str,
    *,
    subject: str,
) -> None:
    """Raise when the transition is not allowed, naming the subject for the operator."""
    if current == target:
        msg = f"{subject} 已经处于 {current} 状态"
        raise InvalidStateTransition(msg)
    if can_transition(table, current, target):
        return
    msg = f"{subject} 不能从 {current} 变为 {target}"
    raise InvalidStateTransition(msg)
