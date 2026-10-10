"""Unit tests for explicit message filtering."""

from xianyu_agent.services.message_filter import MessageFilter


def test_empty_filter_is_fail_open() -> None:
    assert MessageFilter(blocked_patterns=[]).evaluate("任意买家消息").allowed is True


def test_filter_matches_configured_pattern_case_insensitively() -> None:
    result = MessageFilter(blocked_patterns=["REFUND"]).evaluate("Need a refund")

    assert result.allowed is False
    assert result.reason == "匹配消息过滤规则 #1"
