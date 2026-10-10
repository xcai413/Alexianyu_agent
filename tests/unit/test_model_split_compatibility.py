"""Regression checks for the Phase 0 ORM model split."""

from xianyu_agent.db import models as legacy_models

EXPECTED_TABLES = {
    "accounts",
    "ai_providers",
    "alert_dispatches",
    "audit_logs",
    "batch_publish_entries",
    "batch_publish_jobs",
    "buyer_notes",
    "card_consumptions",
    "cards",
    "api_delivery_configs",
    "consumer_inbox",
    "conversations",
    "cookies",
    "daemon_instances",
    "delivery_policies",
    "delivery_guard_rules",
    "personal_blacklist",
    "platform_automation_configs",
    "platform_automation_runs",
    "quick_replies",
    "notification_bindings",
    "notification_channels",
    "delivery_attempts",
    "delivery_jobs",
    "delivery_templates",
    "inventory_pools",
    "inventory_reservations",
    "inventory_units",
    "items",
    "item_variants",
    "item_spec_bindings",
    "item_materials",
    "messages",
    "message_filter_rules",
    "orders",
    "reply_logs",
    "reply_rules",
    "revoked_web_sessions",
    "runtime_leases",
    "scheduler_work",
    "secret_vault_entries",
    "task_logs",
    "transactional_outbox",
    "worker_commands",
    "worker_status",
    "ws_credentials",
}


def test_legacy_model_module_reexports_canonical_classes() -> None:
    """Existing staged callers resolve to canonical split ORM definitions."""
    assert (
        legacy_models.AIProviderConfig.__module__
        == "xianyu_agent.infrastructure.database.models.ai"
    )
    assert legacy_models.Account.__module__ == "xianyu_agent.infrastructure.database.models.account"
    assert (
        legacy_models.WorkerStatus.__module__
        == "xianyu_agent.infrastructure.database.models.runtime"
    )
    assert legacy_models.Message.__module__ == "xianyu_agent.infrastructure.database.models.message"
    assert legacy_models.Conversation.__module__ == (
        "xianyu_agent.infrastructure.database.models.conversation"
    )
    assert legacy_models.Item.__module__ == "xianyu_agent.infrastructure.database.models.item"
    assert legacy_models.Order.__module__ == "xianyu_agent.infrastructure.database.models.order"
    assert legacy_models.Card.__module__ == "xianyu_agent.infrastructure.database.models.inventory"
    assert (
        legacy_models.DeliveryPolicy.__module__
        == "xianyu_agent.infrastructure.database.models.delivery"
    )
    assert (
        legacy_models.DeliveryJob.__module__
        == "xianyu_agent.infrastructure.database.models.delivery"
    )
    assert (
        legacy_models.DeliveryAttempt.__module__
        == "xianyu_agent.infrastructure.database.models.delivery"
    )
    assert (
        legacy_models.DeliveryGuardRule.__module__
        == "xianyu_agent.infrastructure.database.models.delivery"
    )
    assert (
        legacy_models.PersonalBlacklist.__module__
        == "xianyu_agent.infrastructure.database.models.delivery"
    )
    assert (
        legacy_models.ApiDeliveryConfigRow.__module__
        == "xianyu_agent.infrastructure.database.models.delivery"
    )
    assert (
        legacy_models.InventoryPool.__module__
        == "xianyu_agent.infrastructure.database.models.inventory"
    )
    assert (
        legacy_models.InventoryUnit.__module__
        == "xianyu_agent.infrastructure.database.models.inventory"
    )
    assert (
        legacy_models.InventoryReservation.__module__
        == "xianyu_agent.infrastructure.database.models.inventory"
    )
    assert legacy_models.ReplyRule.__module__ == "xianyu_agent.infrastructure.database.models.rules"
    assert legacy_models.AuditLog.__module__ == "xianyu_agent.infrastructure.database.models.audit"
    assert legacy_models.TransactionalOutbox.__module__ == (
        "xianyu_agent.infrastructure.database.models.outbox"
    )
    assert legacy_models.PlatformAutomationConfig.__module__ == (
        "xianyu_agent.infrastructure.database.models.automation"
    )
    assert legacy_models.PlatformAutomationRun.__module__ == (
        "xianyu_agent.infrastructure.database.models.automation"
    )


def test_split_model_package_registers_complete_existing_metadata() -> None:
    """The structural split must register every current table exactly once."""
    assert set(legacy_models.Base.metadata.tables) == EXPECTED_TABLES


def test_worker_status_enum_name_collision_is_removed() -> None:
    """ORM WorkerStatus and persisted status values have distinct symbols."""
    assert legacy_models.WorkerStatus.__tablename__ == "worker_status"
    assert legacy_models.WorkerStatusValue.OFFLINE.value == "offline"
