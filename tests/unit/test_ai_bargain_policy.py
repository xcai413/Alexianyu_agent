"""Unit tests for the AI bargain policy: parsing, floor and round protection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from xianyu_agent.domain.ai.bargain_policy import (
    BELOW_FLOOR_LABEL,
    ROUNDS_EXHAUSTED_LABEL,
    UNVERIFIABLE_LABEL,
    BargainPolicy,
    extract_discount_ratios,
    extract_price_amounts,
    parse_amount,
)

LIST_PRICE = Decimal("50")


def test_extract_price_amounts_reads_every_supported_price_form() -> None:
    assert extract_price_amounts("亲,给您45元包邮哦") == (Decimal("45.00"),)
    assert extract_price_amounts("¥45.5 可以吗") == (Decimal("45.50"),)
    assert extract_price_amounts("四十五元") == (Decimal("45.00"),)
    assert extract_price_amounts("大概88块钱") == (Decimal("88.00"),)
    assert extract_price_amounts("最低35元,不能再少了") == (Decimal("35.00"),)


@pytest.mark.parametrize(
    "reply",
    ["0元", "00.00块钱", "¥0", "￥ 0.00", "0圆", "零元", "〇块", "零零元", "零拾元", "０元"],
)
def test_zero_amounts_are_offers_and_cannot_bypass_a_positive_floor(reply: str) -> None:
    assert extract_price_amounts(reply) == (Decimal("0.00"),)
    for list_price in (LIST_PRICE, None):
        decision = BargainPolicy(minimum_price=Decimal("40")).evaluate_reply(
            reply, list_price=list_price
        )
        assert decision.allowed is False
        assert decision.reason == BELOW_FLOOR_LABEL
        assert decision.offer_price == Decimal("0.00")
        assert decision.counts_as_round is False


@pytest.mark.parametrize(
    "reply", ["0折", "0.0折", "00折", "零折", "〇折", "零零折", "〇零折", "零拾折", "０折"]
)
def test_zero_discounts_are_offers_and_cannot_bypass_the_floor(reply: str) -> None:
    assert extract_discount_ratios(reply) == (Decimal(0),)
    policy = BargainPolicy(minimum_price=Decimal("40"))
    known = policy.evaluate_reply(reply, list_price=LIST_PRICE)
    unknown = policy.evaluate_reply(reply, list_price=None)
    assert known.allowed is False
    assert known.reason == BELOW_FLOOR_LABEL
    assert known.offer_price == Decimal("0.00")
    assert known.counts_as_round is False
    assert unknown.allowed is False
    assert unknown.reason == UNVERIFIABLE_LABEL


@pytest.mark.parametrize("reply", ["50元,也可0元", "¥50或零元", "50元,给您打0折"])
def test_a_positive_offer_does_not_hide_a_zero_offer(reply: str) -> None:
    decision = BargainPolicy(minimum_price=Decimal("40")).evaluate_reply(
        reply, list_price=LIST_PRICE
    )
    assert decision.allowed is False
    assert decision.offer_price == Decimal("0.00")
    assert decision.reason == BELOW_FLOOR_LABEL


@pytest.mark.parametrize("reply", ["0元", "0折"])
@pytest.mark.parametrize("minimum", [None, Decimal("40")])
def test_zero_offers_cannot_bypass_exhausted_rounds(reply: str, minimum: Decimal | None) -> None:
    decision = BargainPolicy(minimum_price=minimum, max_rounds=0).evaluate_reply(
        reply, list_price=LIST_PRICE
    )
    assert decision.allowed is False
    assert decision.reason == ROUNDS_EXHAUSTED_LABEL
    assert decision.floor_price == LIST_PRICE
    assert decision.offer_price == Decimal("0.00")
    assert decision.counts_as_round is False


@pytest.mark.parametrize("reply", ["0元", "0折"])
@pytest.mark.parametrize("minimum", [None, Decimal(0)])
def test_a_permitted_zero_offer_counts_as_a_concession(reply: str, minimum: Decimal | None) -> None:
    decision = BargainPolicy(minimum_price=minimum).evaluate_reply(reply, list_price=LIST_PRICE)
    assert decision.allowed is True
    assert decision.offer_price == Decimal("0.00")
    assert decision.counts_as_round is True


@pytest.mark.parametrize("value", ["0", "0.00", "0元", "零元", "０", "-5"])  # noqa: RUF001 - 覆盖全角零
def test_zero_offers_do_not_make_stored_zero_or_negative_prices_valid(value: str) -> None:
    assert parse_amount(value) is None


@pytest.mark.parametrize("reply", ["商品50元,运费0元", "0元不卖,最低50元"])
def test_every_stated_amount_is_checked_without_semantic_exemptions(reply: str) -> None:
    # 当前契约是保守检查每个金额,不能靠邮费/否定词跳过同文的低价承诺。
    decision = BargainPolicy(minimum_price=Decimal("40")).evaluate_reply(
        reply, list_price=LIST_PRICE
    )
    assert decision.allowed is False
    assert decision.reason == BELOW_FLOOR_LABEL


def test_extract_price_amounts_ignores_text_without_a_price() -> None:
    assert extract_price_amounts("亲,有什么可以帮您") == ()
    assert extract_price_amounts("48小时内发货") == ()
    assert extract_price_amounts("12:30 在线") == ()
    assert extract_price_amounts("") == ()


def test_extract_discount_ratios_reads_discount_forms() -> None:
    assert extract_discount_ratios("给您打8折") == (Decimal("0.8"),)
    assert extract_discount_ratios("八五折,不能再低了") == (Decimal("0.85"),)
    assert extract_discount_ratios("85折") == (Decimal("0.85"),)
    assert extract_discount_ratios("9.5折") == (Decimal("0.95"),)


def test_extract_discount_ratios_drops_values_that_are_not_discounts() -> None:
    assert extract_discount_ratios("这个不打折") == ()
    assert extract_discount_ratios("10折 就是原价") == ()
    assert extract_discount_ratios("20块钱") == ()


def test_parse_amount_reads_mirror_price_strings() -> None:
    assert parse_amount("45") == Decimal("45.00")
    assert parse_amount(" 9.9 ") == Decimal("9.90")
    assert parse_amount("45元") == Decimal("45.00")
    assert parse_amount("") is None
    assert parse_amount(None) is None
    assert parse_amount("abc") is None
    assert parse_amount("0") is None
    assert parse_amount("-5") is None


def test_floor_binds_the_strictest_configured_discount() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.2"), max_discount_amount=Decimal("15"))
    assert policy.floor_price(LIST_PRICE) == Decimal("40.00")


def test_floor_never_goes_below_the_absolute_minimum_price() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.5"), minimum_price=Decimal("30"))
    assert policy.floor_price(LIST_PRICE) == Decimal("30.00")


def test_floor_is_unknown_when_nothing_is_configured() -> None:
    assert BargainPolicy().floor_price(LIST_PRICE) is None
    assert BargainPolicy(minimum_price=Decimal("40")).floor_price(None) == Decimal("40.00")


def test_offer_below_the_floor_is_blocked() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.2"))
    decision = policy.evaluate_reply("给您35元包邮", list_price=LIST_PRICE)
    assert decision.allowed is False
    assert decision.reason == BELOW_FLOOR_LABEL
    assert decision.offer_price == Decimal("35.00")
    assert decision.counts_as_round is False


def test_offer_at_the_floor_is_allowed_and_counts_a_round() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.2"))
    decision = policy.evaluate_reply("给您40元包邮", list_price=LIST_PRICE)
    assert decision.allowed is True
    assert decision.floor_price == Decimal("40.00")
    assert decision.counts_as_round is True


def test_repeating_the_listed_price_costs_no_round() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.2"))
    decision = policy.evaluate_reply("这个就是50元哦", list_price=LIST_PRICE)
    assert decision.allowed is True
    assert decision.counts_as_round is False


def test_reply_without_a_price_needs_no_policy_decision() -> None:
    decision = BargainPolicy().evaluate_reply("亲,有什么可以帮您", list_price=LIST_PRICE)
    assert decision.allowed is True
    assert decision.offer_price is None
    assert decision.counts_as_round is False


def test_exhausted_rounds_leave_no_room_for_a_concession() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.2"), max_rounds=2, current_round=2)
    blocked = policy.evaluate_reply("给您45元", list_price=LIST_PRICE)
    assert blocked.allowed is False
    assert blocked.reason == ROUNDS_EXHAUSTED_LABEL
    allowed = policy.evaluate_reply("给您50元", list_price=LIST_PRICE)
    assert allowed.allowed is True
    assert allowed.counts_as_round is False


def test_discount_without_a_known_list_price_cannot_be_verified() -> None:
    policy = BargainPolicy(max_discount_ratio=Decimal("0.2"))
    decision = policy.evaluate_reply("给您打8折", list_price=None)
    assert decision.allowed is False
    assert decision.reason == UNVERIFIABLE_LABEL


def test_absolute_offer_without_a_list_price_still_uses_the_floor() -> None:
    policy = BargainPolicy(minimum_price=Decimal("40"))
    assert policy.evaluate_reply("给您45元", list_price=None).allowed is True
    blocked = policy.evaluate_reply("给您38元", list_price=None)
    assert blocked.allowed is False
    assert blocked.reason == BELOW_FLOOR_LABEL


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_rounds": -1},
        {"current_round": -1},
        {"max_discount_ratio": Decimal("1.5")},
        {"max_discount_amount": Decimal("-1")},
        {"minimum_price": Decimal("-1")},
    ],
)
def test_policy_rejects_invalid_limits(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BargainPolicy(**overrides)
