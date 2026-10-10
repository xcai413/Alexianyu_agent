"""发货模板的安全渲染(Issue #5 Phase 6, M21)。

只做白名单变量的字面替换:没有 eval、没有表达式、没有属性访问、没有 Jinja。模板里出现白名单
以外的花括号内容一律报错,而不是"原样发出去"——发错内容给买家比发送失败更糟。

替换是一次性的:卡密正文里若含 {order_id} 这种字面量,不会被二次替换。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

ALLOWED_VARIABLES: tuple[str, ...] = (
    "buyer_name",
    "item_title",
    "sku_name",
    "order_id",
    "quantity",
    "card",
)

MAX_TEMPLATE_LENGTH = 2000
MAX_TEMPLATE_NAME_LENGTH = 128

_OPEN = "{"
_CLOSE = "}"


class TemplateError(ValueError):
    """模板不合法或渲染所需变量缺失。"""


@dataclass(frozen=True)
class TemplateParts:
    """A validated template: literal chunks plus ordered variable names."""

    literals: tuple[str, ...]
    variables: tuple[str, ...]

    @property
    def uses_variables(self) -> bool:
        return bool(self.variables)


def parse_template(template: str) -> TemplateParts:
    """Split a template into literals and whitelisted variable names."""
    literals: list[str] = []
    variables: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(template):
        char = template[index]
        if char == _OPEN:
            end = template.find(_CLOSE, index + 1)
            if end == -1:
                msg = f"模板第 {index + 1} 个字符处有未闭合的 '{{'"
                raise TemplateError(msg)
            name = template[index + 1 : end].strip()
            if not name:
                msg = f"模板第 {index + 1} 个字符处是空占位符"
                raise TemplateError(msg)
            if name not in ALLOWED_VARIABLES:
                msg = f"模板变量 {name!r} 不在白名单内,只允许 {'、'.join(ALLOWED_VARIABLES)}"
                raise TemplateError(msg)
            literals.append("".join(buffer))
            buffer = []
            variables.append(name)
            index = end + 1
            continue
        if char == _CLOSE:
            msg = f"模板第 {index + 1} 个字符处有多余的 '}}'"
            raise TemplateError(msg)
        buffer.append(char)
        index += 1
    literals.append("".join(buffer))
    return TemplateParts(literals=tuple(literals), variables=tuple(variables))


def render_template(template: str, variables: Mapping[str, str]) -> str:
    """Render one template; every used variable must be provided."""
    parts = parse_template(template)
    missing = [name for name in parts.variables if name not in variables]
    if missing:
        msg = f"渲染缺少变量: {'、'.join(missing)}"
        raise TemplateError(msg)
    chunks: list[str] = []
    for position, literal in enumerate(parts.literals):
        chunks.append(literal)
        if position < len(parts.variables):
            chunks.append(str(variables[parts.variables[position]]))
    return "".join(chunks)


def check_template(template: str) -> tuple[str, ...]:
    """Validate a template and return the variables it needs (empty means literal)."""
    return parse_template(template).variables


@dataclass(frozen=True)
class DeliveryTemplate:
    """可复用的发货文案模板(参考 Helper 的发货模板库)。

    模板只是文案:内容仍受白名单变量校验,不执行任何表达式。多条发货策略可以引用同一个模板,
    这样改一次文案就能同时影响所有引用它的策略。
    """

    name: str
    content: str
    description: str | None = None
    enabled: bool = True
    local_id: int | None = None
    variables: tuple[str, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            msg = "模板名称不能为空"
            raise TemplateError(msg)
        if len(name) > MAX_TEMPLATE_NAME_LENGTH:
            msg = f"模板名称不能超过 {MAX_TEMPLATE_NAME_LENGTH} 个字符"
            raise TemplateError(msg)
        content = self.content.strip()
        if not content:
            msg = "模板内容不能为空"
            raise TemplateError(msg)
        if len(content) > MAX_TEMPLATE_LENGTH:
            msg = f"模板内容不能超过 {MAX_TEMPLATE_LENGTH} 个字符"
            raise TemplateError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "模板 id 必须为正整数"
            raise TemplateError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "content", content)
        if self.description is not None:
            object.__setattr__(self, "description", self.description.strip() or None)
        # 变量在构造时校验:非法模板进不了库,发货时就不会因为模板报错而中断。
        object.__setattr__(self, "variables", check_template(content))

    def describe(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"{self.name}(#{self.local_id or '-'} {state})"
