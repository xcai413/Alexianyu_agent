"""Bargain policy: the business floor under AI-negotiated prices.

Model output is the only untrusted negotiation source: a reply may promise a
price the operator never authorized, so every price the model states is checked
against the operator's floor before the reply is allowed out. The policy is
pure domain logic (no IO, no asyncio): it receives the item list price and the
offers parsed out of a reply, and returns a decision carrying a named rule
label. Matched text never leaves this module, so audit rows store a reason
instead of model wording.

Known boundaries:

- The item mirror price column is read as a decimal amount in 元, matching the
  existing item fixtures. The real protocol unit is not yet calibrated in
  docs/protocol-notes.md, so this floor must not be relied on until P1 protocol
  calibration confirms the unit.
- Only Arabic digits, the ¥ / 元 / 块 forms, Chinese numerals below 10000 and
  the 折 discount form are recognized. Text the policy cannot parse is not
  treated as a price, so the policy cannot block what it cannot read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

DEFAULT_MAX_ROUNDS = 3
MONEY_QUANTUM = Decimal("0.01")
_MAX_SANE_AMOUNT = Decimal("1000000")

BELOW_FLOOR_LABEL = "议价低于底价"
ROUNDS_EXHAUSTED_LABEL = "议价轮次已用尽"
UNVERIFIABLE_LABEL = "无法校验议价价格"

_CN_DIGITS = {
    "零": 0,
    "〇": 0,  # noqa: RUF001 - 中文数字零的另一种写法
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_CHARS = "".join(sorted({*_CN_DIGITS, *_CN_UNITS}))

_NUMBER = rf"(?:\d+(?:\.\d+)?|[{_CN_CHARS}]+)"
_AMOUNT_RE = re.compile(rf"[¥￥]\s*(?P<prefix>{_NUMBER})|(?P<suffix>{_NUMBER})\s*(?:元|块钱|块|圆)")
_DISCOUNT_RE = re.compile(rf"(?P<value>\d+(?:\.\d+)?|[{_CN_CHARS}]+)\s*折")


def parse_chinese_number(text: str) -> Decimal | None:
    """Parse a Chinese numeral below 10000; return None when it is not one.

    Ambiguous colloquial forms such as 一百二 read literally (102). Reading a
    stated price lower than intended only tightens the floor, which fails in the
    safe direction for the seller.
    """
    if not text:
        return None
    section = 0
    pending: int | None = None
    seen = False
    for character in text:
        if character in _CN_DIGITS:
            pending = _CN_DIGITS[character]
            seen = True
        elif character in _CN_UNITS:
            section += (pending if pending is not None else 1) * _CN_UNITS[character]
            pending = None
            seen = True
        else:
            return None
    if pending is not None:
        section += pending
    if not seen:
        return None
    return Decimal(section)


def parse_amount(text: str | None) -> Decimal | None:
    """Parse a stored or stated amount such as 45, 45.5 or 45元."""
    if text is None:
        return None
    candidate = text.strip()
    if not candidate:
        return None
    try:
        amount: Decimal | None = Decimal(candidate)
    except InvalidOperation:
        amount = parse_chinese_number(candidate)
        if amount is None:
            extracted = extract_price_amounts(candidate)
            amount = extracted[0] if extracted else None
    if amount is None or amount <= 0 or amount > _MAX_SANE_AMOUNT:
        return None
    return _quantize(amount)


def extract_price_amounts(reply_text: str) -> tuple[Decimal, ...]:
    """Return absolute amounts stated in a reply, in the order they appear."""
    amounts: list[Decimal] = []
    for match in _AMOUNT_RE.finditer(reply_text):
        token = match.group("prefix") or match.group("suffix")
        amount = _parse_token(token)
        if amount is not None:
            amounts.append(amount)
    return tuple(amounts)


def extract_discount_ratios(reply_text: str) -> tuple[Decimal, ...]:
    """Return the paid fraction of each 折 discount stated in a reply.

    8折 becomes 0.8, 八五折 and 85折 become 0.85. Zero is a full concession
    and must reach the floor check. Values outside [0, 1) are dropped.
    """
    ratios: list[Decimal] = []
    for match in _DISCOUNT_RE.finditer(reply_text):
        ratio = _parse_discount(match.group("value"))
        if ratio is not None:
            ratios.append(ratio)
    return tuple(ratios)


@dataclass(frozen=True)
class BargainDecision:
    """Outcome of validating the price offers stated by one reply."""

    allowed: bool
    reason: str | None = None
    floor_price: Decimal | None = None
    offer_price: Decimal | None = None
    counts_as_round: bool = False


@dataclass(frozen=True)
class BargainPolicy:
    """Operator limits for one negotiated conversation."""

    minimum_price: Decimal | None = None
    max_discount_ratio: Decimal | None = None
    max_discount_amount: Decimal | None = None
    max_rounds: int = DEFAULT_MAX_ROUNDS
    current_round: int = 0

    def __post_init__(self) -> None:
        if self.max_rounds < 0:
            raise ValueError("max_rounds 不能为负数")
        if self.current_round < 0:
            raise ValueError("current_round 不能为负数")
        if self.max_discount_ratio is not None and not (
            Decimal(0) <= self.max_discount_ratio <= Decimal(1)
        ):
            raise ValueError("max_discount_ratio 必须在 0 到 1 之间")
        if self.max_discount_amount is not None and self.max_discount_amount < 0:
            raise ValueError("max_discount_amount 不能为负数")
        if self.minimum_price is not None and self.minimum_price < 0:
            raise ValueError("minimum_price 不能为负数")

    def rounds_exhausted(self) -> bool:
        """Whether the conversation has used up its allowed bargaining rounds."""
        return self.current_round >= self.max_rounds

    def floor_price(self, list_price: Decimal | None) -> Decimal | None:
        """Lowest price the operator allows, ignoring the round guard.

        max_discount_ratio and max_discount_amount both cap the concession, so
        the strictest of the two binds. minimum_price is an absolute floor and
        wins whenever it is higher.
        """
        floor: Decimal | None = None
        if list_price is not None:
            allowed_discounts: list[Decimal] = []
            if self.max_discount_ratio is not None:
                allowed_discounts.append(list_price * self.max_discount_ratio)
            if self.max_discount_amount is not None:
                allowed_discounts.append(self.max_discount_amount)
            if allowed_discounts:
                floor = list_price - min(allowed_discounts)
        if self.minimum_price is not None:
            floor = self.minimum_price if floor is None else max(floor, self.minimum_price)
        if floor is None:
            return None
        return _quantize(max(floor, Decimal(0)))

    def effective_floor(self, list_price: Decimal | None) -> Decimal | None:
        """Floor actually enforced now; once rounds run out no concession is left."""
        floor = self.floor_price(list_price)
        if self.rounds_exhausted() and list_price is not None:
            floor = list_price if floor is None else max(floor, list_price)
            floor = _quantize(floor)
        return floor

    def evaluate_reply(
        self,
        reply_text: str,
        *,
        list_price: Decimal | None,
    ) -> BargainDecision:
        """Decide whether a reply may state the prices it states."""
        amounts = extract_price_amounts(reply_text)
        ratios = extract_discount_ratios(reply_text)
        floor = self.floor_price(list_price)
        if ratios and list_price is None:
            # 折扣无法换算成金额,也没有绝对底价可比,因此不放行。
            return BargainDecision(
                False,
                UNVERIFIABLE_LABEL,
                floor_price=floor,
                counts_as_round=False,
            )
        offers = list(amounts)
        if list_price is not None:
            offers.extend(_quantize(list_price * ratio) for ratio in ratios)
        if not offers:
            return BargainDecision(True, floor_price=floor)

        offer = min(offers)
        effective = self.effective_floor(list_price)
        if effective is not None and offer < effective:
            reason = (
                ROUNDS_EXHAUSTED_LABEL
                if self.rounds_exhausted() and (floor is None or effective > floor)
                else BELOW_FLOOR_LABEL
            )
            return BargainDecision(
                False,
                reason,
                floor_price=effective,
                offer_price=offer,
                counts_as_round=False,
            )
        return BargainDecision(
            True,
            floor_price=effective,
            offer_price=offer,
            counts_as_round=_is_concession(offer, list_price),
        )


def _is_concession(offer: Decimal, list_price: Decimal | None) -> bool:
    """Whether stating this price concedes ground and consumes a bargain round."""
    if list_price is None:
        return True
    return offer < list_price


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _parse_token(token: str) -> Decimal | None:
    # 中文数字解析可能返回 None,所以这里显式声明可空,避免把 None 赋给 Decimal 变量。
    amount: Decimal | None
    try:
        amount = Decimal(token)
    except InvalidOperation:
        amount = parse_chinese_number(token)
    # 零报价也可能违反底价;存储商品价格的正数要求仍由parse_amount单独检查。
    if amount is None or amount < 0 or amount > _MAX_SANE_AMOUNT:
        return None
    return _quantize(amount)


def _parse_discount(token: str) -> Decimal | None:
    # 两个分支都会用 parsed,提前声明为可空,避免在分支里重复注解或把 None 赋给 Decimal。
    parsed: Decimal | None
    # ratio 在"中文数字解析失败"时为 None,不能在分支里先被推断成 Decimal。
    ratio: Decimal | None
    if all(character in _CN_DIGITS for character in token):
        if len(token) == 1:
            ratio = Decimal(_CN_DIGITS[token]) / Decimal(10)
        elif len(token) == 2:
            ratio = Decimal(f"{_CN_DIGITS[token[0]]}.{_CN_DIGITS[token[1]]}") / Decimal(10)
        else:
            parsed = parse_chinese_number(token)
            ratio = None if parsed is None else parsed / Decimal(10)
    else:
        parsed = parse_chinese_number(token)
        if parsed is None:
            try:
                parsed = Decimal(token)
            except InvalidOperation:
                return None
        # 8折 是 0.8;85折 是 8.5 折的常见写法;10折 即不打折,不作为折扣处理。
        if parsed == Decimal(10):
            return None
        ratio = parsed / Decimal(100) if parsed > 10 else parsed / Decimal(10)
    if ratio is None or not Decimal(0) <= ratio < 1:
        return None
    return ratio
