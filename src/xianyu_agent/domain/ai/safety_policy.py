"""Outbound safety policy for AI-generated replies.

AI text is the only untrusted reply source: model output may expose the
assistant identity, offer an off-platform contact or link the buyer away from
Xianyu. Rule replies come from administrator configuration and are not filtered
here.

The policy stays small and explicit. It blocks a reply only on a named,
testable rule and reports the rule label instead of the matched text, so audit
rows never carry model output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MAX_REPLY_LENGTH = 300
IDENTITY_LABEL = "AI 身份暴露"
CONTACT_LABEL = "站外联系方式"
LINK_LABEL = "站外链接"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        IDENTITY_LABEL,
        re.compile(
            r"(我是|身为|作为)[^。！？!?\n]{0,8}(AI|人工智能|智能助手|语言模型|大模型|机器人)"  # noqa: RUF001
            r"|AI\s*(助手|客服)|人工智能助手|语言模型|大模型",
            re.IGNORECASE,
        ),
    ),
    (
        CONTACT_LABEL,
        re.compile(
            r"(微信|微\s*信|weixin|wechat|vx|v\s*信|扣扣|QQ|企鹅号)\s*(号|是|[:：])?\s*[A-Za-z0-9_\-]{4,}"  # noqa: RUF001
            r"|1[3-9]\d{9}"
            r"|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
            re.IGNORECASE,
        ),
    ),
    (LINK_LABEL, re.compile(r"https?://|www\.", re.IGNORECASE)),
)


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str | None = None


class SafetyPolicy:
    """Block AI replies that break built-in rules or operator-added patterns."""

    def __init__(
        self,
        *,
        extra_patterns: Iterable[str] | None = None,
        max_length: int = MAX_REPLY_LENGTH,
    ) -> None:
        if max_length <= 0:
            raise ValueError("回复长度上限必须为正数")
        self._max_length = max_length
        self._extra_patterns = tuple(
            pattern.strip().casefold() for pattern in (extra_patterns or ()) if pattern.strip()
        )

    def evaluate(self, text: str) -> SafetyDecision:
        candidate = (text or "").strip()
        if not candidate:
            return SafetyDecision(False, "模型返回空回复")
        for label, pattern in _PATTERNS:
            if pattern.search(candidate):
                return SafetyDecision(False, f"命中输出安全规则: {label}")
        folded = candidate.casefold()
        for index, needle in enumerate(self._extra_patterns, start=1):
            if needle in folded:
                return SafetyDecision(False, f"命中输出安全规则: 自定义规则 #{index}")
        if len(candidate) > self._max_length:
            return SafetyDecision(False, f"回复超长({len(candidate)} > {self._max_length})")
        return SafetyDecision(True)
