"""Regression tests for the reply-rule domain responsibility migration."""

from xianyu_agent.cli.commands import rule as rule_cli
from xianyu_agent.domain import rules as legacy_rules
from xianyu_agent.domain.message import rules
from xianyu_agent.services import reply_engine


def test_legacy_rules_module_aliases_canonical_message_rules_module() -> None:
    assert legacy_rules is rules
    assert legacy_rules.DEFAULT_PRIORITY == rules.DEFAULT_PRIORITY
    assert legacy_rules.create_rule is rules.create_rule
    assert legacy_rules.match_for_account is rules.match_for_account
    assert legacy_rules.record_hit is rules.record_hit
    assert legacy_rules.record_reply_log is rules.record_reply_log


def test_reply_rule_callers_use_canonical_message_rules_module() -> None:
    assert rule_cli.domain_rules is rules
    assert reply_engine.domain_rules is rules
