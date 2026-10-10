"""BargainGate: check the prices an AI reply commits to against the operator floor.

SafetyPolicy screens what a reply says; this gate screens what a reply promises.
It resolves the item list price, the rounds the conversation already used and
the configured limits, then applies BargainPolicy to the model output. A reply
that concedes ground consumes one round; a blocked reply consumes none, so a
rejected offer cannot silently burn the operator's allowance.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from xianyu_agent.config import get_settings
from xianyu_agent.domain.ai import BargainDecision, BargainPolicy, parse_amount
from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.message import messages as domain_messages

logger = logging.getLogger(__name__)


class BargainGate:
    """Apply the configured bargain limits to one AI reply."""

    def __init__(
        self,
        *,
        minimum_price: Decimal | None = None,
        max_discount_ratio: Decimal | None = None,
        max_discount_amount: Decimal | None = None,
        max_rounds: int | None = None,
    ) -> None:
        settings = get_settings()
        self._minimum_price = (
            minimum_price
            if minimum_price is not None
            else _configured_amount(settings.bargain_minimum_price)
        )
        self._max_discount_ratio = (
            max_discount_ratio
            if max_discount_ratio is not None
            else _configured_ratio(settings.bargain_max_discount_ratio)
        )
        self._max_discount_amount = (
            max_discount_amount
            if max_discount_amount is not None
            else _configured_amount(settings.bargain_max_discount_amount)
        )
        self._max_rounds = max_rounds if max_rounds is not None else settings.bargain_max_rounds

    def policy(self, *, current_round: int) -> BargainPolicy:
        """Build the policy for one conversation at its current round."""
        return BargainPolicy(
            minimum_price=self._minimum_price,
            max_discount_ratio=self._max_discount_ratio,
            max_discount_amount=self._max_discount_amount,
            max_rounds=self._max_rounds,
            current_round=current_round,
        )

    async def check(
        self,
        *,
        account_id: str,
        chat_id: str,
        item_id: str | None,
        reply_text: str,
    ) -> BargainDecision:
        """Validate one AI reply, consuming a round only for an allowed concession."""
        list_price = await self._list_price(account_id, item_id)
        current_round = await domain_messages.get_bargain_round(
            account_id=account_id, chat_id=chat_id
        )
        decision = self.policy(current_round=current_round).evaluate_reply(
            reply_text, list_price=list_price
        )
        if not decision.allowed:
            logger.info(
                "AI 回复被议价保护拦截 account=%s chat=%s reason=%s",
                account_id,
                chat_id,
                decision.reason,
            )
            return decision
        if decision.counts_as_round:
            await domain_messages.advance_bargain_round(account_id=account_id, chat_id=chat_id)
        return decision

    @staticmethod
    async def _list_price(account_id: str, item_id: str | None) -> Decimal | None:
        """Read the mirrored list price; unknown means the floor cannot be derived."""
        if not item_id:
            return None
        item = await domain_items.get_item_by_external_id(account_id, item_id)
        if item is None:
            return None
        return parse_amount(item.price)


def _configured_amount(value: float) -> Decimal | None:
    amount = Decimal(str(value))
    return amount if amount > 0 else None


def _configured_ratio(value: float) -> Decimal | None:
    ratio = Decimal(str(value))
    return ratio if ratio > 0 else None
