"""发货模板的安全渲染(Issue #5 Phase 6, M21)。"""

from __future__ import annotations

import pytest

from xianyu_agent.domain.delivery import (
    ALLOWED_VARIABLES,
    TemplateError,
    check_template,
    render_template,
)


def test_literal_template_needs_no_variables() -> None:
    assert check_template("固定文案,没有占位符") == ()
    assert render_template("固定文案", {}) == "固定文案"


def test_every_whitelisted_variable_renders() -> None:
    values = {name: f"<{name}>" for name in ALLOWED_VARIABLES}
    template = (
        "买家{buyer_name} 商品{item_title} 规格{sku_name} 单号{order_id} 数量{quantity} 卡密{card}"
    )

    rendered = render_template(template, values)

    assert "<buyer_name>" in rendered
    assert "<card>" in rendered
    assert check_template(template) == ALLOWED_VARIABLES


def test_unknown_variable_is_rejected() -> None:
    with pytest.raises(TemplateError, match="不在白名单内"):
        check_template("你好{user_name}")


def test_attribute_access_and_format_specs_are_rejected() -> None:
    with pytest.raises(TemplateError, match="不在白名单内"):
        check_template("{order_id.__class__}")
    with pytest.raises(TemplateError, match="不在白名单内"):
        check_template("{quantity:>3}")
    with pytest.raises(TemplateError, match="不在白名单内"):
        check_template("{card[0]}")


def test_broken_braces_are_rejected() -> None:
    with pytest.raises(TemplateError, match="未闭合"):
        check_template("卡密:{card")
    with pytest.raises(TemplateError, match="多余的"):
        check_template("卡密:card}")
    with pytest.raises(TemplateError, match="空占位符"):
        check_template("卡密:{}")
    with pytest.raises(TemplateError, match="不在白名单内"):
        check_template("{{card}}")


def test_missing_value_fails_before_sending() -> None:
    with pytest.raises(TemplateError, match="缺少变量"):
        render_template("卡密:{card}", {"order_id": "1"})


def test_substitution_is_single_pass() -> None:
    """卡密正文里的花括号是字面量,不会被二次替换。"""
    rendered = render_template("卡密:{card}", {"card": "{order_id}"})

    assert rendered == "卡密:{order_id}"


def test_repeated_variable_uses_each_position() -> None:
    rendered = render_template("{card} 与 {card}", {"card": "A1"})

    assert rendered == "A1 与 A1"
