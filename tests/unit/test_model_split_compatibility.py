"""Regression checks for the Phase 0 ORM model split."""

from xianyu_agent.db import models as legacy_models

EXPECTED_TABLES = {
    "accounts",
    "audit_logs",
    "card_consumptions",
    "cards",
    "cookies",
    "daemon_instances",
    "items",
    "messages",
    "orders",
    "reply_logs",
    "reply_rules",
    "task_logs",
    "transactional_outbox",
    "worker_commands",
    "worker_status",
    "ws_credentials",
}


def test_legacy_model_module_reexports_canonical_classes() -> None:
    """Existing staged callers resolve to canonical split ORM definitions."""
    assert legacy_models.Account.__module__ == "xianyu_agent.infrastructure.database.models.account"
    assert legacy_models.WorkerStatus.__module__ == "xianyu_agent.infrastructure.database.models.runtime"
    assert legacy_models.Message.__module__ == "xianyu_agent.infrastructure.database.models.message"
    assert legacy_models.Item.__module__ == "xianyu_agent.infrastructure.database.models.item"
    assert legacy_models.Order.__module__ == "xianyu_agent.infrastructure.database.models.order"
    assert legacy_models.Card.__module__ == "xianyu_agent.infrastructure.database.models.inventory"
    assert legacy_models.ReplyRule.__module__ == "xianyu_agent.infrastructure.database.models.rules"
    assert legacy_models.AuditLog.__module__ == "xianyu_agent.infrastructure.database.models.audit"
    assert legacy_models.TransactionalOutbox.__module__ == (
        "xianyu_agent.infrastructure.database.models.outbox"
    )


def test_split_model_package_registers_complete_existing_metadata() -> None:
    """The structural split must register every current table exactly once."""
    assert set(legacy_models.Base.metadata.tables) == EXPECTED_TABLES


def test_worker_status_enum_name_collision_is_removed() -> None:
    """ORM WorkerStatus and persisted status values have distinct symbols."""
    assert legacy_models.WorkerStatus.__tablename__ == "worker_status"
    assert legacy_models.WorkerStatusValue.OFFLINE.value == "offline"
