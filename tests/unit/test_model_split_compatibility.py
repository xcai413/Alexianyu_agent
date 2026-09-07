"""Regression checks for the Phase 0 ORM model split."""

from xianyu_agent.db import models as legacy_models
from xianyu_agent.infrastructure.database import models as canonical_models


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
    "worker_commands",
    "worker_status",
    "ws_credentials",
}


def test_legacy_model_module_reexports_canonical_classes() -> None:
    """Existing staged callers keep identical ORM class objects during migration."""
    assert legacy_models.Base is canonical_models.Base
    assert legacy_models.Account is canonical_models.Account
    assert legacy_models.WorkerStatus is canonical_models.WorkerStatus
    assert legacy_models.Message is canonical_models.Message
    assert legacy_models.Item is canonical_models.Item
    assert legacy_models.Order is canonical_models.Order
    assert legacy_models.Card is canonical_models.Card
    assert legacy_models.ReplyRule is canonical_models.ReplyRule
    assert legacy_models.AuditLog is canonical_models.AuditLog


def test_split_model_package_registers_complete_existing_metadata() -> None:
    """The structural split must neither lose nor duplicate existing tables."""
    assert set(canonical_models.Base.metadata.tables) == EXPECTED_TABLES


def test_worker_status_enum_name_collision_is_removed() -> None:
    """ORM WorkerStatus and persisted status values have distinct symbols."""
    assert canonical_models.WorkerStatus.__tablename__ == "worker_status"
    assert canonical_models.WorkerStatusValue.OFFLINE.value == "offline"
