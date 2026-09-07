"""Contract tests for shared Foundation context/error/pagination types."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from xianyu_agent.foundation import (
    API_PATH_PREFIX,
    API_VERSION,
    ActorContext,
    ActorSource,
    CausationId,
    CommandContext,
    ConfirmationChallenge,
    CorrelationId,
    ErrorCode,
    IdempotencyKey,
    MachineError,
    PageRequest,
    PageResult,
    RequestId,
    SortDirection,
    SortSpec,
    ensure_utc,
)


def test_identifier_types_are_non_empty_and_semantically_distinct() -> None:
    request_id = RequestId.new()
    correlation_id = CorrelationId.new()
    assert request_id.value
    assert correlation_id.value
    assert request_id != correlation_id
    with pytest.raises(ValueError, match="must not be empty"):
        IdempotencyKey("   ")


def test_command_context_generates_ids_and_validates_timezone() -> None:
    actor = ActorContext(ActorSource.MCP, actor_id=" operator ", roles=frozenset({" admin "}))
    context = CommandContext.create(
        actor,
        causation_id=CausationId("event-1"),
        idempotency_key=IdempotencyKey("idem-1"),
        timezone_name="Asia/Shanghai",
    )
    assert context.actor.actor_id == "operator"
    assert context.actor.roles == frozenset({"admin"})
    assert context.request_id.value
    assert context.correlation_id.value
    with pytest.raises(ValueError, match="unknown timezone"):
        CommandContext.create(actor, timezone_name="Mars/Olympus")


def test_actor_sources_are_frozen_for_all_entrypoints() -> None:
    assert {source.value for source in ActorSource} == {
        "web",
        "cli",
        "mcp",
        "tui",
        "automation",
        "system",
    }


def test_page_request_normalizes_query_and_has_bounded_semantics() -> None:
    request = PageRequest(
        page=3,
        page_size=25,
        sort=(SortSpec("created_at", SortDirection.DESC),),
        query="  buyer  ",
    )
    assert request.offset == 50
    assert request.query == "buyer"
    result = PageResult(items=("a", "b"), total=51, page=3, page_size=25)
    assert result.pages == 3
    with pytest.raises(ValueError, match="between 1 and 200"):
        PageRequest(page_size=201)


def test_machine_error_serializes_stable_safe_shape() -> None:
    correlation_id = CorrelationId("corr-1")
    error = MachineError(
        ErrorCode.REQUIRE_CONFIRMATION,
        "confirmation required",
        correlation_id,
        details={"challenge_id": "challenge-1"},
    )
    assert error.to_payload() == {
        "error": {
            "code": "REQUIRE_CONFIRMATION",
            "message": "confirmation required",
            "correlation_id": "corr-1",
            "details": {"challenge_id": "challenge-1"},
        }
    }
    assert {code.value for code in ErrorCode} == {
        "VALIDATION_FAILED",
        "NOT_FOUND",
        "CONFLICT",
        "NEEDS_VALIDATION",
        "REQUIRE_CONFIRMATION",
        "BLOCKED",
        "RETRYABLE",
        "UNCERTAIN",
        "RECONCILIATION_REQUIRED",
    }


def test_confirmation_challenge_uses_utc_and_expires_deterministically() -> None:
    now = datetime(2026, 9, 8, 4, 0, tzinfo=timezone(timedelta(hours=8)))
    challenge = ConfirmationChallenge.create(
        action="delete_account",
        message="confirm deletion",
        correlation_id=CorrelationId("corr-2"),
        ttl=timedelta(minutes=5),
        now=now,
    )
    assert challenge.expires_at.tzinfo is UTC
    assert challenge.is_expired(now=now + timedelta(minutes=4)) is False
    assert challenge.is_expired(now=now + timedelta(minutes=5)) is True


def test_persistence_time_contract_rejects_naive_and_normalizes_to_utc() -> None:
    aware = datetime(2026, 9, 8, 4, 0, tzinfo=timezone(timedelta(hours=8)))
    assert ensure_utc(aware) == datetime(2026, 9, 7, 20, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(datetime(2026, 9, 8, 4, 0))


def test_api_versioning_contract_is_explicit() -> None:
    assert API_VERSION == "v1"
    assert API_PATH_PREFIX == "/api/v1"
