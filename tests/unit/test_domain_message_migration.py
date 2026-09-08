"""Regression tests for the message domain responsibility migration."""

from xianyu_agent.cli.commands import message as message_cli
from xianyu_agent.domain import messages as legacy_messages
from xianyu_agent.domain.message import messages
from xianyu_agent.runtime import account_worker
from xianyu_agent.services import reply_engine


def test_legacy_messages_module_aliases_canonical_message_module() -> None:
    assert legacy_messages is messages
    assert legacy_messages.upsert_inbound is messages.upsert_inbound
    assert legacy_messages.record_outbound is messages.record_outbound
    assert legacy_messages.list_recent is messages.list_recent
    assert legacy_messages.get_by_id is messages.get_by_id


def test_message_callers_use_canonical_message_module() -> None:
    assert message_cli.domain_messages is messages
    assert reply_engine.domain_messages is messages
    assert account_worker.domain_messages is messages
