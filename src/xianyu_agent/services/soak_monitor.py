"""P0-E 可恢复的 24 小时长稳采样与验收汇总。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select

from xianyu_agent.config import get_settings
from xianyu_agent.db import (
    Account,
    Cookie,
    DaemonInstance,
    Message,
    ReplyLog,
    TaskLog,
    get_async_session,
)
from xianyu_agent.db.models import TaskStatus
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.services.logging_setup import redact_text
from xianyu_agent.services.observability import RuntimeSnapshot, build_runtime_snapshot

TASK_NAME = "p0-e-soak"
DEFAULT_INTERVAL_S = 60.0
MIN_COVERAGE = 0.9
MAX_GAP_FACTOR = 2.5


@dataclass(frozen=True)
class SoakRun:
    task_id: int
    status: str
    account_id: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    params: dict[str, object]


async def start_soak(*, account_id: str, hours: float = 24.0) -> SoakRun:
    """创建一个持久化长稳运行;每分钟由 Watchdog 调用采样。"""
    if hours <= 0:
        msg = "hours must be positive"
        raise ValueError(msg)
    existing = await active_soak()
    if existing is not None:
        msg = f"已有运行中的长稳验收:{existing.run_id}"
        raise RuntimeError(msg)
    account = await domain_accounts.get_account(account_id)
    if account is None:
        msg = f"account not found: {account_id}"
        raise ValueError(msg)
    snapshot = await build_runtime_snapshot()
    observed = _account_from(snapshot, account_id)
    if not snapshot.daemon_health.healthy or not observed.aligned or observed.actual_state != "connected":
        msg = (
            f"启动条件不满足:daemon={snapshot.daemon_health.observed},"
            f"account={observed.actual_state},aligned={observed.aligned}"
        )
        raise RuntimeError(msg)

    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex
    params = {
        "schema_version": 1,
        "run_id": run_id,
        "account_id": account_id,
        "target_duration_s": hours * 3600,
        "interval_s": DEFAULT_INTERVAL_S,
        "deadline_at": (now + timedelta(hours=hours)).isoformat(),
        "initial_daemon_instance": snapshot.daemon.instance_id if snapshot.daemon else None,
        "initial_daemon_pid": snapshot.daemon.pid if snapshot.daemon else None,
        "sample_count": 0,
        "healthy_samples": 0,
        "error_samples": 0,
        "stale_samples": 0,
        "outage_count": 0,
        "outage_started_at": None,
        "max_outage_s": 0.0,
        "max_sample_gap_s": 0.0,
        "max_daemon_heartbeat_age_s": 0.0,
        "max_worker_heartbeat_age_s": 0.0,
        "max_reconnects": observed.reconnect_attempts,
        "daemon_instance_changes": 0,
        "unexpected_daemon_starts": 0,
        "duplicate_reply_groups": 0,
        "inbound_messages": 0,
        "outbound_messages": 0,
        "successful_replies": 0,
        "failed_replies": 0,
        "sensitive_log_hits": 0,
        "sensitive_log_files": [],
        "log_offsets": {},
        "issues": [],
    }
    async with get_async_session() as session:
        row = TaskLog(
            task_name=TASK_NAME,
            account_id=account.id,
            status=TaskStatus.RUNNING,
            params=params,
            started_at=now,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        task_id = row.id
    await sample_soak(task_id)
    result = await get_soak(task_id)
    if result is None:  # pragma: no cover - row just created
        msg = "soak row disappeared"
        raise RuntimeError(msg)
    return result


async def sample_active_soaks() -> int:
    """采样全部运行中的长稳任务;单项失败会落库,不影响 Watchdog。"""
    async with get_async_session() as session:
        ids = list(
            (
                await session.execute(
                    select(TaskLog.id)
                    .where(TaskLog.task_name == TASK_NAME)
                    .where(TaskLog.status == TaskStatus.RUNNING)
                )
            ).scalars()
        )
    for task_id in ids:
        try:
            await sample_soak(task_id)
        except Exception as exc:
            await _mark_monitor_error(task_id, exc)
    return len(ids)


async def sample_soak(task_id: int) -> None:
    run = await get_soak(task_id)
    if run is None or run.status != TaskStatus.RUNNING:
        return
    now = datetime.now(UTC)
    snapshot = await build_runtime_snapshot(now=now)
    account = _account_from(snapshot, run.account_id)
    metrics = await _business_metrics(run.started_at, run.account_id)
    unexpected_starts = await _unexpected_daemon_starts(run.started_at)
    previous = dict(run.params)
    hits, files, offsets = await _scan_log_changes(
        {str(key): int(value) for key, value in dict(previous.get("log_offsets") or {}).items()}
    )
    sample = _build_sample(now, snapshot, account)
    _append_evidence(run.run_id, sample)
    updated = _aggregate(
        previous,
        sample,
        metrics=metrics,
        unexpected_daemon_starts=unexpected_starts,
        sensitive_hits=hits,
        sensitive_files=files,
        log_offsets=offsets,
    )
    deadline = _parse_utc(str(updated["deadline_at"]))
    finished = now >= deadline
    status = TaskStatus.RUNNING
    error: str | None = None
    finished_at: datetime | None = None
    if finished:
        issues = _final_issues(updated, started_at=run.started_at, now=now)
        updated["issues"] = issues
        status = TaskStatus.SUCCESS if not issues else TaskStatus.FAILED
        error = "; ".join(issues) or None
        finished_at = now
    async with get_async_session() as session:
        row = await session.get(TaskLog, task_id)
        if row is None or row.status != TaskStatus.RUNNING:
            return
        row.params = updated
        row.status = status
        row.error = error
        row.finished_at = finished_at
        await session.commit()


async def active_soak() -> SoakRun | None:
    async with get_async_session() as session:
        row = (
            await session.execute(
                select(TaskLog)
                .where(TaskLog.task_name == TASK_NAME)
                .where(TaskLog.status == TaskStatus.RUNNING)
                .order_by(TaskLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return _to_run(row)


async def latest_soak() -> SoakRun | None:
    async with get_async_session() as session:
        row = (
            await session.execute(
                select(TaskLog)
                .where(TaskLog.task_name == TASK_NAME)
                .order_by(TaskLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return _to_run(row)


async def get_soak(task_id: int) -> SoakRun | None:
    async with get_async_session() as session:
        row = await session.get(TaskLog, task_id)
    return _to_run(row) if row and row.task_name == TASK_NAME else None


async def stop_soak() -> SoakRun | None:
    run = await active_soak()
    if run is None:
        return None
    now = datetime.now(UTC)
    async with get_async_session() as session:
        row = await session.get(TaskLog, run.task_id)
        if row is None:
            return None
        params = dict(row.params or {})
        params["issues"] = [*list(params.get("issues") or []), "用户提前停止"]
        row.params = params
        row.status = TaskStatus.SKIPPED
        row.error = "用户提前停止"
        row.finished_at = now
        await session.commit()
    return await get_soak(run.task_id)


def evidence_path(run_id: str) -> Path:
    return get_settings().log_dir / f"p0-e-soak-{run_id}.jsonl"


def _build_sample(now: datetime, snapshot: RuntimeSnapshot, account) -> dict[str, object]:
    healthy = (
        snapshot.daemon_health.healthy
        and account.actual_state == "connected"
        and account.aligned
        and not account.last_error
        and not snapshot.operational_alerts
    )
    return {
        "at": now.isoformat(),
        "daemon_instance": snapshot.daemon.instance_id if snapshot.daemon else None,
        "daemon_pid": snapshot.daemon.pid if snapshot.daemon else None,
        "daemon": snapshot.daemon_health.observed,
        "daemon_heartbeat_age_s": snapshot.daemon_health.heartbeat_age_s,
        "account": account.account_id,
        "desired": account.desired_state,
        "actual": account.actual_state,
        "aligned": account.aligned,
        "worker_heartbeat_age_s": account.heartbeat_age_s,
        "reconnect_attempts": account.reconnect_attempts,
        "has_error": bool(account.last_error),
        "error": redact_text(account.last_error) if account.last_error else None,
        "healthy": healthy,
        "operational_alerts": [redact_text(alert) for alert in snapshot.operational_alerts],
    }


def _aggregate(
    params: dict[str, object],
    sample: dict[str, object],
    *,
    metrics: dict[str, int],
    unexpected_daemon_starts: int,
    sensitive_hits: int,
    sensitive_files: list[str],
    log_offsets: dict[str, int],
) -> dict[str, object]:
    updated = dict(params)
    count = int(updated.get("sample_count") or 0) + 1
    updated["sample_count"] = count
    updated["healthy_samples"] = int(updated.get("healthy_samples") or 0) + int(
        bool(sample["healthy"])
    )
    updated["error_samples"] = int(updated.get("error_samples") or 0) + int(
        not bool(sample["healthy"])
    )
    is_stale = sample["daemon"] == "stale" or sample["actual"] == "stale"
    updated["stale_samples"] = int(updated.get("stale_samples") or 0) + int(is_stale)
    previous_at = updated.get("last_sample_at")
    gap = 0.0 if not previous_at else (_parse_utc(str(sample["at"])) - _parse_utc(str(previous_at))).total_seconds()
    updated["max_sample_gap_s"] = max(float(updated.get("max_sample_gap_s") or 0.0), gap)
    updated["last_sample_at"] = sample["at"]
    updated["last_daemon"] = sample["daemon"]
    updated["last_actual"] = sample["actual"]
    outage_started_at = updated.get("outage_started_at")
    if sample["healthy"]:
        if outage_started_at:
            outage_s = (
                _parse_utc(str(sample["at"])) - _parse_utc(str(outage_started_at))
            ).total_seconds()
            updated["max_outage_s"] = max(
                float(updated.get("max_outage_s") or 0.0), outage_s
            )
            updated["outage_started_at"] = None
    elif not outage_started_at:
        updated["outage_started_at"] = sample["at"]
        updated["outage_count"] = int(updated.get("outage_count") or 0) + 1
    updated["max_daemon_heartbeat_age_s"] = max(
        float(updated.get("max_daemon_heartbeat_age_s") or 0.0),
        float(sample["daemon_heartbeat_age_s"] or 0.0),
    )
    updated["max_worker_heartbeat_age_s"] = max(
        float(updated.get("max_worker_heartbeat_age_s") or 0.0),
        float(sample["worker_heartbeat_age_s"] or 0.0),
    )
    updated["max_reconnects"] = max(
        int(updated.get("max_reconnects") or 0), int(sample["reconnect_attempts"] or 0)
    )
    updated["daemon_instance_changes"] = int(updated.get("daemon_instance_changes") or 0) + int(
        sample["daemon_instance"] != updated.get("initial_daemon_instance")
        and sample["daemon_instance"] != updated.get("last_changed_instance")
    )
    if sample["daemon_instance"] != updated.get("initial_daemon_instance"):
        updated["last_changed_instance"] = sample["daemon_instance"]
    updated["unexpected_daemon_starts"] = unexpected_daemon_starts
    updated.update(metrics)
    updated["sensitive_log_hits"] = int(updated.get("sensitive_log_hits") or 0) + sensitive_hits
    updated["sensitive_log_files"] = sorted(
        {str(item) for item in list(updated.get("sensitive_log_files") or [])}
        | set(sensitive_files)
    )
    updated["log_offsets"] = log_offsets
    return updated


def _final_issues(params: dict[str, object], *, started_at: datetime, now: datetime) -> list[str]:
    issues: list[str] = []
    target = float(params["target_duration_s"])
    interval = float(params["interval_s"])
    elapsed = (now - _aware_utc(started_at)).total_seconds()
    expected = max(1, int(target / interval))
    if params.get("outage_started_at"):
        outage_s = (now - _parse_utc(str(params["outage_started_at"]))).total_seconds()
        params["max_outage_s"] = max(float(params.get("max_outage_s") or 0.0), outage_s)
    if elapsed < target:
        issues.append("实际时长不足")
    if int(params["sample_count"]) < int(expected * MIN_COVERAGE):
        issues.append("采样覆盖率不足 90%")
    if float(params["max_sample_gap_s"]) > interval * MAX_GAP_FACTOR:
        issues.append("采样中断超过阈值")
    if int(params["error_samples"]):
        issues.append("存在 daemon/账号异常样本")
    if int(params.get("monitor_errors") or 0):
        issues.append("监测器自身发生采样错误")
    if int(params["daemon_instance_changes"]) or int(params["unexpected_daemon_starts"]):
        issues.append("daemon 实例发生变化")
    if int(params["duplicate_reply_groups"]):
        issues.append("检测到重复消息回复")
    if int(params["sensitive_log_hits"]):
        issues.append("日志检测到敏感值")
    return issues


async def _business_metrics(started_at: datetime, account_id: str) -> dict[str, int]:
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id))
        ).scalar_one()
        reply_groups = (
            select(ReplyLog.message_id)
            .where(ReplyLog.account_id == account.id)
            .where(ReplyLog.message_id.is_not(None))
            .where(ReplyLog.success.is_(True))
            .where(ReplyLog.sent_at >= started_at)
            .group_by(ReplyLog.message_id)
            .having(func.count(ReplyLog.id) > 1)
            .subquery()
        )
        duplicate_groups = int(
            (await session.execute(select(func.count()).select_from(reply_groups))).scalar_one()
        )
        message_rows = (
            await session.execute(
                select(Message.direction, func.count(Message.id))
                .where(Message.account_id == account.id)
                .where(Message.received_at >= started_at)
                .group_by(Message.direction)
            )
        ).all()
        message_counts = {str(direction): int(count) for direction, count in message_rows}
        reply_rows = (
            await session.execute(
                select(ReplyLog.success, func.count(ReplyLog.id))
                .where(ReplyLog.account_id == account.id)
                .where(ReplyLog.sent_at >= started_at)
                .group_by(ReplyLog.success)
            )
        ).all()
        reply_counts = {bool(success): int(count) for success, count in reply_rows}
    return {
        "duplicate_reply_groups": duplicate_groups,
        "inbound_messages": message_counts.get("inbound", 0),
        "outbound_messages": message_counts.get("outbound", 0),
        "successful_replies": reply_counts.get(True, 0),
        "failed_replies": reply_counts.get(False, 0),
    }


async def _unexpected_daemon_starts(started_at: datetime) -> int:
    async with get_async_session() as session:
        return int(
            (
                await session.execute(
                    select(func.count(DaemonInstance.id)).where(
                        DaemonInstance.started_at > started_at
                    )
                )
            ).scalar_one()
        )


async def _scan_log_changes(offsets: dict[str, int]) -> tuple[int, list[str], dict[str, int]]:
    settings = get_settings()
    secrets = await _sensitive_values()
    hits = 0
    files: list[str] = []
    updated: dict[str, int] = {}
    for path in sorted(settings.log_dir.glob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        old_offset = offsets.get(path.name, 0)
        previous = 0 if size < old_offset else old_offset
        with path.open("rb") as handle:
            handle.seek(previous)
            text = handle.read().decode("utf-8", errors="replace")
        updated[path.name] = size
        file_hits = sum(text.count(secret) for secret in secrets if secret)
        if file_hits:
            hits += file_hits
            files.append(path.name)
    return hits, files, updated


async def _sensitive_values() -> set[str]:
    settings = get_settings()
    values = {settings.fernet_key}
    async with get_async_session() as session:
        rows = list((await session.execute(select(Cookie))).scalars())
    for row in rows:
        values.add(row.encrypted_value)
        try:
            cookie = settings.fernet.decrypt(row.encrypted_value.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError):
            continue
        values.add(cookie)
        for part in cookie.split(";"):
            _key, separator, value = part.strip().partition("=")
            if separator:
                values.add(value)
    return {value for value in values if len(value) >= 8}


async def _mark_monitor_error(task_id: int, exc: Exception) -> None:
    async with get_async_session() as session:
        row = await session.get(TaskLog, task_id)
        if row is None or row.status != TaskStatus.RUNNING:
            return
        params = dict(row.params or {})
        params["monitor_errors"] = int(params.get("monitor_errors") or 0) + 1
        params["last_monitor_error"] = redact_text(f"{type(exc).__name__}: {exc}")
        row.params = params
        await session.commit()


def _append_evidence(run_id: str, sample: dict[str, object]) -> None:
    path = evidence_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(sample, ensure_ascii=False) + "\n")


def _account_from(snapshot: RuntimeSnapshot, account_id: str):
    account = next((row for row in snapshot.accounts if row.account_id == account_id), None)
    if account is None:
        msg = f"account not found in runtime snapshot: {account_id}"
        raise RuntimeError(msg)
    return account


def _to_run(row: TaskLog | None) -> SoakRun | None:
    if row is None:
        return None
    params = dict(row.params or {})
    return SoakRun(
        task_id=row.id,
        status=row.status,
        account_id=str(params.get("account_id") or ""),
        run_id=str(params.get("run_id") or ""),
        started_at=row.started_at,
        finished_at=row.finished_at,
        params=params,
    )


def _parse_utc(value: str) -> datetime:
    return _aware_utc(datetime.fromisoformat(value))


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
