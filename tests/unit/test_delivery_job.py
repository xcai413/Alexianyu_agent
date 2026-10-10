"""发货任务状态机与重试分类(Issue #5 Phase 6, §14/§16)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xianyu_agent.domain.delivery import (
    DEFAULT_MAX_ATTEMPTS,
    AttemptResult,
    DeliveryAttempt,
    DeliveryJob,
    DeliveryJobState,
    FailureKind,
    InvalidJobTransition,
    RetryDecision,
    classify_failure,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _job(**kwargs: object) -> DeliveryJob:
    payload: dict[str, object] = {"account_id": "acc-1", "order_id": "O-1"}
    payload.update(kwargs)
    return DeliveryJob(**payload)  # type: ignore[arg-type]


def test_job_validation_rejects_broken_input() -> None:
    with pytest.raises(ValueError, match="账号"):
        _job(account_id="  ")
    with pytest.raises(ValueError, match="订单"):
        _job(order_id="")
    with pytest.raises(ValueError, match="语义键"):
        _job(semantic_key=" ")
    with pytest.raises(ValueError, match="数量至少为 1"):
        _job(required_quantity=0)
    with pytest.raises(ValueError, match="未知发货任务状态"):
        _job(state="teleported")


def test_happy_path_transitions_and_timestamps() -> None:
    job = _job(local_id=1)

    checking = job.transition_to(DeliveryJobState.CHECKING.value, at=NOW)
    reserving = checking.transition_to(DeliveryJobState.RESERVING.value, at=NOW)
    reserved = reserving.transition_to(DeliveryJobState.RESERVED.value, at=NOW)
    delivering = reserved.transition_to(DeliveryJobState.DELIVERING.value, at=NOW)
    confirming = delivering.transition_to(DeliveryJobState.CONFIRMING.value, at=NOW)
    completed = confirming.transition_to(DeliveryJobState.COMPLETED.value, at=NOW)

    assert completed.state == DeliveryJobState.COMPLETED.value
    assert completed.started_at == NOW
    assert completed.completed_at == NOW
    assert completed.is_terminal is True


def test_illegal_transitions_are_rejected() -> None:
    job = _job(local_id=1)

    with pytest.raises(InvalidJobTransition, match="不能从 pending"):
        job.transition_to(DeliveryJobState.COMPLETED.value)
    with pytest.raises(InvalidJobTransition, match="已经处于 pending"):
        job.transition_to(DeliveryJobState.PENDING.value)

    uncertain = (
        job.transition_to(DeliveryJobState.CHECKING.value)
        .transition_to(DeliveryJobState.RESERVING.value)
        .transition_to(DeliveryJobState.UNCERTAIN.value)
    )
    with pytest.raises(InvalidJobTransition):
        uncertain.transition_to(DeliveryJobState.RESERVED.value)
    assert uncertain.transition_to(DeliveryJobState.COMPLETED.value).state == "completed"


def test_uncertain_can_be_retried_only_after_human_verdict() -> None:
    job = _job(local_id=2)
    uncertain = (
        job.transition_to(DeliveryJobState.CHECKING.value)
        .transition_to(DeliveryJobState.RESERVING.value)
        .transition_to(DeliveryJobState.UNCERTAIN.value)
    )

    retryable = uncertain.transition_to(DeliveryJobState.FAILED_RETRYABLE.value)

    assert retryable.state == DeliveryJobState.FAILED_RETRYABLE.value
    assert retryable.transition_to(DeliveryJobState.CHECKING.value).state == "checking"


def test_classify_failure_matrix() -> None:
    retryable = classify_failure(FailureKind.NOT_SENT, attempt_no=1)
    assert retryable.state == DeliveryJobState.FAILED_RETRYABLE.value
    assert retryable.retryable is True

    final = classify_failure(FailureKind.NOT_SENT, attempt_no=DEFAULT_MAX_ATTEMPTS)
    assert final.state == DeliveryJobState.FAILED_FINAL.value
    assert final.retryable is False

    rejected = classify_failure(FailureKind.REJECTED, attempt_no=1)
    assert rejected.state == DeliveryJobState.FAILED_FINAL.value

    blocked = classify_failure(FailureKind.BLOCKED, attempt_no=1)
    assert blocked.state == DeliveryJobState.BLOCKED.value
    assert blocked.requires_human is True

    unknown = classify_failure(FailureKind.UNKNOWN, attempt_no=1)
    assert unknown.state == DeliveryJobState.UNCERTAIN.value
    assert unknown.retryable is False
    assert "冻结" in unknown.reason


def test_classify_failure_rejects_bad_attempt_number() -> None:
    with pytest.raises(ValueError, match="从 1 开始"):
        classify_failure(FailureKind.NOT_SENT, attempt_no=0)


def test_max_attempts_is_configurable() -> None:
    decision = classify_failure(FailureKind.NOT_SENT, attempt_no=2, max_attempts=2)

    assert decision.state == DeliveryJobState.FAILED_FINAL.value


def test_retry_decision_human_flags() -> None:
    assert RetryDecision(state="uncertain", retryable=False, reason="").requires_human is True
    assert (
        RetryDecision(state="failed_retryable", retryable=True, reason="").requires_human is False
    )


def test_attempt_validation() -> None:
    attempt = DeliveryAttempt(
        job_id=1, attempt_no=1, action="send", result=AttemptResult.SUCCESS.value
    )

    assert attempt.result == "success"
    with pytest.raises(ValueError, match="已存在的发货任务"):
        DeliveryAttempt(job_id=0, attempt_no=1, action="send", result="success")
    with pytest.raises(ValueError, match="从 1 开始"):
        DeliveryAttempt(job_id=1, attempt_no=0, action="send", result="success")
    with pytest.raises(ValueError, match="动作"):
        DeliveryAttempt(job_id=1, attempt_no=1, action=" ", result="success")
    with pytest.raises(ValueError, match="结果"):
        DeliveryAttempt(job_id=1, attempt_no=1, action="send", result="")
