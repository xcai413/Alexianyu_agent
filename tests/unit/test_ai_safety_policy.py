"""Unit tests for outbound AI reply safety rules."""

import pytest

from xianyu_agent.domain.ai import SafetyPolicy


def test_benign_reply_is_allowed() -> None:
    assert SafetyPolicy().evaluate("亲,这件是全新未拆封的,今天就能发货哦").allowed is True


def test_blank_reply_is_blocked() -> None:
    decision = SafetyPolicy().evaluate("   ")

    assert decision.allowed is False
    assert decision.reason == "模型返回空回复"


def test_reply_over_length_limit_is_blocked() -> None:
    decision = SafetyPolicy(max_length=10).evaluate("好" * 11)

    assert decision.allowed is False
    assert decision.reason == "回复超长(11 > 10)"


def test_length_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="回复长度上限必须为正数"):
        SafetyPolicy(max_length=0)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("我是一个人工智能助手,可以帮你解答", "AI 身份暴露"),
        ("加我微信 abc12345,给你便宜点", "站外联系方式"),
        ("也可以联系 13800138000", "站外联系方式"),
        ("邮箱 seller@example.com 详聊", "站外联系方式"),
        ("看这个链接 https://example.com/item", "站外链接"),
    ],
)
def test_builtin_rules_block_and_name_the_category(text: str, label: str) -> None:
    decision = SafetyPolicy().evaluate(text)

    assert decision.allowed is False
    assert decision.reason == f"命中输出安全规则: {label}"


def test_extra_patterns_block_case_insensitively() -> None:
    decision = SafetyPolicy(extra_patterns=["包退"]).evaluate("放心,坏了包退")

    assert decision.allowed is False
    assert decision.reason == "命中输出安全规则: 自定义规则 #1"
