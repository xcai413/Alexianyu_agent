"""发货策略与解析优先级(Issue #5 Phase 6, §9)。

策略回答"这个订单一次发货发什么、什么时候发":它指向一个库存池,可以带延迟、模板和
平台确认标记。范围由字段决定,越具体越优先:规格 > 商品 > 账号 > 全局。解析结果必须确定,
所以同级别的平局按 priority 高者优先、再按 id 小者优先,并把命中的层级与依据一起返回,
方便审计回答"为什么用了这条策略"。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from xianyu_agent.domain.inventory import POOL_TYPES

from .template import check_template

MAX_DELAY_SECONDS = 3600

LEVEL_SKU = "sku"
LEVEL_ITEM = "item"
LEVEL_ACCOUNT = "account"
LEVEL_GLOBAL = "global"

LEVEL_ORDER: tuple[str, ...] = (LEVEL_SKU, LEVEL_ITEM, LEVEL_ACCOUNT, LEVEL_GLOBAL)


class PolicyError(ValueError):
    """策略配置不合法。"""


@dataclass(frozen=True)
class DeliveryPolicy:
    """One delivery policy, scoped by the fields that are set."""

    name: str
    inventory_pool_id: int
    account_id: str | None = None
    item_id: str | None = None
    sku_id: str | None = None
    delay_seconds: int = 0
    delivery_mode: str | None = None
    template: str | None = None
    # 可选的模板库引用(name 或 id 字符串);与内联 template 同时存在时以内联为准。
    template_ref: str | None = None
    platform_confirm: bool = False
    priority: int = 0
    enabled: bool = True
    description: str | None = None
    local_id: int | None = None
    template_variables: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "策略名称不能为空"
            raise PolicyError(msg)
        if self.inventory_pool_id <= 0:
            msg = "策略必须指向一个已存在的库存池"
            raise PolicyError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "策略 id 必须为正整数"
            raise PolicyError(msg)
        if not 0 <= self.delay_seconds <= MAX_DELAY_SECONDS:
            msg = f"延迟发货秒数必须在 0 到 {MAX_DELAY_SECONDS} 之间"
            raise PolicyError(msg)
        if self.delivery_mode is not None and self.delivery_mode not in POOL_TYPES:
            msg = f"未知发货模式: {self.delivery_mode!r},可选 {POOL_TYPES}"
            raise PolicyError(msg)
        if self.sku_id is not None and self.item_id is None:
            msg = "规格级策略必须同时指定商品"
            raise PolicyError(msg)
        if self.priority < 0:
            msg = "优先级不能为负数"
            raise PolicyError(msg)
        if self.template is not None and not self.template.strip():
            # 空白模板多半是误操作:静默当成"不用模板"会改变发给买家的内容。
            msg = "模板不能是空白字符串(不用模板请留空)"
            raise PolicyError(msg)
        for field_name in (
            "account_id",
            "item_id",
            "sku_id",
            "template",
            "template_ref",
            "description",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, value.strip() or None)
        if self.template is not None:
            object.__setattr__(self, "template_variables", check_template(self.template))
            if "card" not in self.template_variables:
                # 不带 {card} 的模板会把"没有卡密的发货消息"发给买家。
                msg = "发货模板必须包含 {card},否则买家收不到卡密"
                raise PolicyError(msg)

    @property
    def level(self) -> str:
        if self.sku_id is not None:
            return LEVEL_SKU
        if self.item_id is not None:
            return LEVEL_ITEM
        if self.account_id is not None:
            return LEVEL_ACCOUNT
        return LEVEL_GLOBAL

    def scope_key(self) -> str:
        """Stable identity of the scope this policy claims."""
        return (
            "|".join(
                f"{name}={value}"
                for name, value in (
                    ("account", self.account_id),
                    ("item", self.item_id),
                    ("sku", self.sku_id),
                )
                if value is not None
            )
            or "global"
        )

    def describe(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"{self.name}(#{self.local_id or '-'} {self.level} {state} -> pool:{self.inventory_pool_id})"


@dataclass(frozen=True)
class PolicyResolution:
    """Which policy won for a delivery request, and the reasoning trail."""

    policy: DeliveryPolicy | None
    level: str | None
    reason: str
    candidates: int = 0

    @property
    def matched(self) -> bool:
        return self.policy is not None


def validate_policies(policies: Sequence[DeliveryPolicy]) -> tuple[str, ...]:
    """Return problems that make a policy set ambiguous; empty means fine."""
    problems: list[str] = []
    seen: set[str] = set()
    for policy in policies:
        if not policy.enabled:
            continue
        key = f"{policy.level}:{policy.scope_key()}"
        if key in seen:
            problems.append(f"同级别的范围重复配置: {policy.describe()}")
        seen.add(key)
    return tuple(problems)


def resolve_policy(
    policies: Iterable[DeliveryPolicy],
    *,
    account_id: str | None = None,
    item_id: str | None = None,
    sku_id: str | None = None,
) -> PolicyResolution:
    """Pick the most specific enabled policy for one request."""
    enabled = [policy for policy in policies if policy.enabled]
    considered = len(enabled)
    for level in LEVEL_ORDER:
        matches = [
            policy
            for policy in enabled
            if policy.level == level and _scope_matches(policy, account_id, item_id, sku_id)
        ]
        if not matches:
            continue
        winner = sorted(matches, key=lambda policy: (-policy.priority, policy.local_id or 0))[0]
        reason = f"命中 {level} 级策略(priority={winner.priority},候选 {len(matches)} 条)"
        return PolicyResolution(policy=winner, level=level, reason=reason, candidates=considered)
    return PolicyResolution(
        policy=None, level=None, reason="没有任何启用的策略匹配本次发货", candidates=considered
    )


def _scope_matches(
    policy: DeliveryPolicy,
    account_id: str | None,
    item_id: str | None,
    sku_id: str | None,
) -> bool:
    if policy.level == LEVEL_SKU:
        return policy.sku_id == sku_id and policy.item_id == item_id
    if policy.level == LEVEL_ITEM:
        if policy.item_id != item_id:
            return False
        return policy.account_id is None or policy.account_id == account_id
    if policy.level == LEVEL_ACCOUNT:
        return policy.account_id == account_id
    return True
