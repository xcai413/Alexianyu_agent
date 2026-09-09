"""ReplyEngine: after an inbound message is persisted, match rules and send a reply.

The sender callback is injected by the caller (AccountWorker wires it to the
WsClient's send_text). Sending failures are recorded in reply_logs with
success=False so they can be retried or audited.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from xianyu_agent.domain.events import MessageReceived
from xianyu_agent.domain.message import messages as domain_messages, rules as domain_rules
from xianyu_agent.services.ai_provider import AIProvider, UnconfiguredError

logger = logging.getLogger(__name__)

Sender = Callable[[str, str, str], Awaitable[bool]]  # (account_id, chat_id, text) -> ok


class ReplyEngine:
    def __init__(
        self,
        sender: Sender | None = None,
        *,
        auto_reply: bool = True,
        provider: AIProvider | None = None,
        mode: Literal["rule", "ai", "rule_then_ai"] = "rule",
    ) -> None:
        self._sender = sender
        self.auto_reply = auto_reply
        self._provider = provider
        self.mode = mode

    async def handle(
        self,
        event: MessageReceived,
        *,
        message_id: int | None = None,
    ) -> None:
        """Match rules for the message; if one hits, send the reply and log it."""
        if not self.auto_reply:
            return
        reply_text: str | None = None
        rule_id: int | None = None
        source = "rule"
        chat_context = await self._chat_context(event.account_id, event.chat_id)

        rules = await domain_rules.match_for_account(event.account_id, event.content)
        rule = rules[0] if rules else None

        if self.mode == "rule" and rule is not None:
            reply_text = rule.reply_text
            rule_id = rule.id
            await domain_rules.record_hit(rule.id)
        elif self.mode == "rule_then_ai":
            if rule is not None:
                reply_text = rule.reply_text
                rule_id = rule.id
                await domain_rules.record_hit(rule.id)
            else:
                reply_text, rule_id, source = await self._ai_reply(event, chat_context)
        elif self.mode == "ai":
            reply_text, rule_id, source = await self._ai_reply(event, chat_context)

        if reply_text is None:
            logger.debug(
                "no reply for account=%s chat=%s (mode=%s)",
                event.account_id,
                event.chat_id,
                self.mode,
            )
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

    async def _ai_reply(
        self, event: MessageReceived, chat_context: list[str]
    ) -> tuple[str | None, int | None, str]:
        if self._provider is None:
            return None, None, "ai"
        try:
            text = await self._provider.generate_reply(
                account_id=event.account_id,
                buyer_message=event.content,
                chat_context=chat_context,
            )
        except UnconfiguredError:
            logger.debug("AI provider unconfigured; skipping ai reply")
            return None, None, "ai"
        except Exception as exc:
            logger.warning("AI reply failed: %s", exc)
            return None, None, "ai"
        if not text:
            return None, None, "ai"
        return text, None, "ai"

    async def _chat_context(self, account_id: str, chat_id: str) -> list[str]:
        rows = await domain_messages.list_recent(account_id=account_id, chat_id=chat_id, limit=6)
        return [m.content or "" for m in reversed(rows)]
