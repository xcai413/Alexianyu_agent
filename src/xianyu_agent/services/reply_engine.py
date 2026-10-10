"""ReplyEngine: after an inbound message is persisted, match rules and send a reply.

The sender callback is injected by the caller. AccountWorker wires it through
the canonical runtime message-sender bridge. Sending failures are recorded in
reply_logs with success=False so they can be retried or audited.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from xianyu_agent.application.ai import ContextService
from xianyu_agent.application.chat import ChatAssistService
from xianyu_agent.config import get_settings
from xianyu_agent.db.models import RuleType
from xianyu_agent.domain.account import state as domain_accounts
from xianyu_agent.domain.ai import (
    AIConversationContext,
    AIProvider,
    AIRequest,
    SafetyPolicy,
    UnconfiguredError,
)
from xianyu_agent.domain.events import MessageReceived
from xianyu_agent.domain.message import messages as domain_messages, rules as domain_rules
from xianyu_agent.services.bargain_gate import BargainGate
from xianyu_agent.services.message_filter import MessageFilter
from xianyu_agent.services.reply_decision_audit import record_reply_decision

logger = logging.getLogger(__name__)

Sender = Callable[[str, str, str], Awaitable[bool]]  # (account_id, chat_id, text) -> ok
# 图片端口:第三个参数是 https 图片地址,语义与文本端口一致(返回是否确认发出)。
ImageSender = Callable[[str, str, str], Awaitable[bool]]
ReplyPipeline = Literal[
    "RULE_THEN_AI", "RULE_THEN_DEFAULT", "RULE_DEFAULT_THEN_AI", "AI_ONLY", "RULE_ONLY"
]


class ReplyEngine:
    def __init__(
        self,
        sender: Sender | None = None,
        *,
        image_sender: ImageSender | None = None,
        auto_reply: bool = True,
        provider: AIProvider | None = None,
        mode: str | None = None,
        message_filter: MessageFilter | None = None,
        context_service: ContextService | None = None,
        safety_policy: SafetyPolicy | None = None,
        bargain_gate: BargainGate | None = None,
    ) -> None:
        self._sender = sender
        self._image_sender = image_sender
        self.auto_reply = auto_reply
        self._provider = provider
        self.mode: ReplyPipeline = _normalize_pipeline(mode or get_settings().reply_mode)
        self._message_filter = message_filter or MessageFilter()
        self._context_service = context_service or ContextService()
        self._safety_policy = safety_policy or SafetyPolicy()
        self._bargain_gate = bargain_gate or BargainGate()

    async def handle(  # noqa: PLR0911, PLR0912, PLR0915 - one ordered reply-decision pipeline
        self,
        event: MessageReceived,
        *,
        message_id: int | None = None,
    ) -> None:
        """Match rules for the message; if one hits, send the reply and log it."""
        if not self.auto_reply:
            await self._audit(event, message_id, outcome="suppressed", reason="automation_disabled")
            return
        # 账号级暂停优先于会话状态:运营暂停整个账号时(例如正在人工处理风控),
        # 所有会话都应停手,而不是逐个会话去关。
        if await domain_accounts.is_reply_paused(event.account_id):
            logger.debug("auto reply suppressed by account pause account=%s", event.account_id)
            await self._audit(
                event, message_id, outcome="suppressed", reason="account_reply_paused"
            )
            return
        if not await domain_messages.is_auto_reply_enabled(
            account_id=event.account_id, chat_id=event.chat_id
        ):
            logger.debug(
                "auto reply suppressed by conversation state account=%s chat=%s",
                event.account_id,
                event.chat_id,
            )
            await self._audit(event, message_id, outcome="suppressed", reason="conversation_state")
            return
        configured_patterns = await ChatAssistService().enabled_reply_filter_keywords(event.account_id)
        filter_decision = self._message_filter.extend(list(configured_patterns)).evaluate(event.content)
        if not filter_decision.allowed:
            await self._audit(
                event, message_id, outcome="suppressed", reason=filter_decision.reason
            )
            return
        reply_text: str | None = None
        rule_id: int | None = None
        reply_image_url: str | None = None
        source = "rule"
        rules = await domain_rules.match_for_account(
            event.account_id, event.content, item_id=event.item_id
        )
        keyword_rule = next((rule for rule in rules if rule.type != RuleType.DEFAULT.value), None)
        default_rule = next((rule for rule in rules if rule.type == RuleType.DEFAULT.value), None)
        rule = rules[0] if rules else None
        selected_rule = rule
        use_ai = False
        if self.mode == "RULE_THEN_AI":
            selected_rule = keyword_rule
            use_ai = selected_rule is None
        elif self.mode == "RULE_THEN_DEFAULT":
            selected_rule = keyword_rule or default_rule
        elif self.mode == "RULE_DEFAULT_THEN_AI":
            selected_rule = keyword_rule or default_rule
            use_ai = selected_rule is None
        elif self.mode == "AI_ONLY":
            selected_rule = None
            use_ai = True
        if (
            selected_rule is not None
            and selected_rule.reply_once
            and await domain_rules.replied_once_in_conversation(selected_rule.id, message_id)
        ):
            logger.debug("reply_once suppressed rule=%s chat=%s", selected_rule.id, event.chat_id)
            await self._audit(
                event,
                message_id,
                outcome="suppressed",
                reason="reply_once",
                rule_id=selected_rule.id,
            )
            return

        if selected_rule is not None:
            reply_text = selected_rule.reply_text
            rule_id = selected_rule.id
            await domain_rules.record_hit(selected_rule.id)
            reply_image_url = getattr(selected_rule, "reply_image_url", None)
        elif use_ai:
            context = await self._chat_context(
                event.account_id,
                event.chat_id,
                current_message_id=message_id,
                item_id=event.item_id,
            )
            reply_text, rule_id, source = await self._ai_reply(
                event,
                context.messages,
                item_facts=context.item_facts,
            )
            if reply_text is not None:
                safety = self._safety_policy.evaluate(reply_text)
                if not safety.allowed:
                    logger.info(
                        "AI 回复被输出安全规则拦截 account=%s chat=%s",
                        event.account_id,
                        event.chat_id,
                    )
                    await self._audit(
                        event,
                        message_id,
                        outcome="suppressed",
                        reason=safety.reason,
                        source="ai",
                    )
                    return
                bargain = await self._bargain_gate.check(
                    account_id=event.account_id,
                    chat_id=event.chat_id,
                    item_id=event.item_id,
                    reply_text=reply_text,
                )
                if not bargain.allowed:
                    await self._audit(
                        event,
                        message_id,
                        outcome="suppressed",
                        reason=bargain.reason,
                        source="ai",
                    )
                    return

        if reply_text is None:
            logger.debug(
                "no reply for account=%s chat=%s (mode=%s)",
                event.account_id,
                event.chat_id,
                self.mode,
            )
            await self._audit(event, message_id, outcome="no_reply", reason="no_matching_reply")
            return

        ok = False
        error: str | None = None
        if self._sender is not None:
            try:
                ok = await self._sender(event.account_id, event.chat_id, reply_text)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("reply send failed: %s", error)
        log = await domain_rules.record_reply_log(
            account_id=event.account_id,
            message_id=message_id,
            rule_id=rule_id,
            sent_text=reply_text,
            success=ok,
            error=error,
            source=source,
        )
        if log is None:
            logger.warning("reply_log not persisted for account=%s", event.account_id)
        await self._audit(
            event,
            message_id,
            outcome="sent" if ok else "send_failed",
            reason=error,
            source=source,
            rule_id=rule_id,
        )
        if ok and reply_image_url:
            await self._send_rule_image(
                event, message_id, image_url=reply_image_url, rule_id=rule_id
            )

    async def _send_rule_image(
        self,
        event: MessageReceived,
        message_id: int | None,
        *,
        image_url: str,
        rule_id: int | None,
    ) -> None:
        """规则配图在文本之后单独发送;失败只记账,绝不重发文本。

        文本已经发出去是既成事实,重发会打扰买家;图片失败只记一条决策审计,运营可以从
        `attention list` / 审计里看到。
        """
        if self._image_sender is None:
            logger.warning(
                "rule image configured but no image sender account=%s", event.account_id
            )
            await self._audit(
                event,
                message_id,
                outcome="image_unavailable",
                reason="未配置图片发送端口",
                rule_id=rule_id,
            )
            return
        try:
            sent = await self._image_sender(event.account_id, event.chat_id, image_url)
        except Exception as exc:
            logger.warning("rule image send failed account=%s", event.account_id)
            await self._audit(
                event,
                message_id,
                outcome="image_failed",
                reason=f"{type(exc).__name__}",
                rule_id=rule_id,
            )
            return
        await self._audit(
            event,
            message_id,
            outcome="image_sent" if sent else "image_failed",
            reason=None if sent else "图片通道返回失败,结果不明",
            rule_id=rule_id,
        )

    async def _audit(
        self,
        event: MessageReceived,
        message_id: int | None,
        *,
        outcome: str,
        reason: str | None,
        source: str | None = None,
        rule_id: int | None = None,
    ) -> None:
        await record_reply_decision(
            account_id=event.account_id,
            message_id=message_id,
            outcome=outcome,
            reason=reason,
            source=source,
            rule_id=rule_id,
        )

    async def _ai_reply(
        self,
        event: MessageReceived,
        chat_context: list[str],
        *,
        item_facts: str | None,
    ) -> tuple[str | None, int | None, str]:
        if self._provider is None:
            return None, None, "ai"
        try:
            response = await self._provider.generate(
                AIRequest(
                    account_id=event.account_id,
                    buyer_message=event.content,
                    chat_context=tuple(chat_context),
                    item_facts=item_facts,
                )
            )
        except UnconfiguredError:
            logger.debug("AI provider unconfigured; skipping ai reply")
            return None, None, "ai"
        except Exception as exc:
            logger.warning("AI reply failed type=%s", type(exc).__name__)
            return None, None, "ai"
        text = response.text
        if not text:
            return None, None, "ai"
        return text, None, "ai"

    async def _chat_context(
        self,
        account_id: str,
        chat_id: str,
        *,
        current_message_id: int | None,
        item_id: str | None,
    ) -> AIConversationContext:
        return await self._context_service.build(
            account_id=account_id,
            chat_id=chat_id,
            current_message_id=current_message_id,
            item_id=item_id,
        )


def _normalize_pipeline(value: str) -> ReplyPipeline:
    aliases = {"rule": "RULE_ONLY", "ai": "AI_ONLY", "rule_then_ai": "RULE_THEN_AI"}
    normalized = aliases.get(value.casefold(), value.upper())
    valid = {
        "RULE_THEN_AI",
        "RULE_THEN_DEFAULT",
        "RULE_DEFAULT_THEN_AI",
        "AI_ONLY",
        "RULE_ONLY",
    }
    if normalized not in valid:
        raise ValueError(f"未知 Reply Pipeline: {value}")
    return normalized  # type: ignore[return-value]
