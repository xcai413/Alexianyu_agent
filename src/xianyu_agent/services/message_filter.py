"""Explicit, fail-open message filtering before automated replies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from xianyu_agent.config import get_settings


@dataclass(frozen=True)
class MessageFilterDecision:
    allowed: bool
    reason: str | None = None


class MessageFilter:
    """Block only administrator-configured content patterns.

    Empty configuration is intentionally fail-open. Product policy must be
    explicit; this component does not infer a buyer's intent from text.
    """

    def __init__(self, *, blocked_patterns: Iterable[str] | None = None) -> None:
        if blocked_patterns is None:
            blocked_patterns = get_settings().message_filter_block_patterns.split(",")
        self._blocked_patterns = tuple(
            pattern.strip().casefold() for pattern in blocked_patterns if pattern.strip()
        )

    def evaluate(self, content: str) -> MessageFilterDecision:
        candidate = content.casefold()
        for index, pattern in enumerate(self._blocked_patterns, start=1):
            if pattern in candidate:
                return MessageFilterDecision(False, f"匹配消息过滤规则 #{index}")
        return MessageFilterDecision(True)

    def extend(self, patterns: Iterable[str]) -> MessageFilter:
        """返回合并账号规则后的不可变过滤器。"""
        return MessageFilter(blocked_patterns=(*self._blocked_patterns, *patterns))
