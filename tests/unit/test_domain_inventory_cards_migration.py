"""Regression tests for the card inventory domain responsibility migration."""

from xianyu_agent.cli.commands import card as card_cli
from xianyu_agent.domain import cards as legacy_cards
from xianyu_agent.domain.inventory import cards
from xianyu_agent.services import delivery_service


def test_legacy_cards_module_aliases_canonical_inventory_module() -> None:
    assert legacy_cards is cards
    assert legacy_cards.create_card is cards.create_card
    assert legacy_cards.consume_card is cards.consume_card
    assert legacy_cards.list_cards is cards.list_cards


def test_card_cli_and_delivery_use_canonical_inventory_module() -> None:
    assert card_cli.domain_cards is cards
    assert delivery_service.domain_cards is cards
