"""Irreversibly redacted protocol calibration records."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel

from xianyu_agent.protocol.events import (
    ConnectionStateChanged,
    ErrorOccurred,
    EventEnvelope,
    WsFrame,
)
from xianyu_agent.protocol.parser import unpack_sync_payloads

_SAFE_PROTOCOL_VALUES = {
    "sync",
    "text",
    "image",
    "card",
    "product",
    "system",
    "notice",
    "order",
    "inbound",
    "outbound",
    "MsgTips",
}
_PROCESS_SALT = secrets.token_bytes(32)


@dataclass
class CaptureCounts:
    frames: int = 0
    events: int = 0


@dataclass(frozen=True)
class CaptureVerification:
    ok: bool
    records: int
    frames: int
    events: int
    messages: int
    target_reached: bool
    missing_message_fields: tuple[str, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class _ActualCaptureCounts:
    frames: int
    events: int
    messages: int
    system_notices: int
    errors: int
    connected_states: int


_SUMMARY_FIELDS = {
    "connected",
    "frames",
    "events",
    "messages",
    "orders",
    "system_notices",
    "errors",
    "duplicate_message_events",
    "missing_message_fields",
    "target_messages",
    "target_reached",
}
_MESSAGE_REQUIRED_FIELDS = {
    "message_id",
    "chat_id",
    "buyer_id",
    "item_id",
    "sent_at",
    "content",
}


class CalibrationRecorder:
    """Append structural frame/event evidence without storing original string values."""

    def __init__(self, path: Path, *, account_id: str) -> None:
        self.path = path
        self.account_id = account_id
        self.counts = CaptureCounts()
        self._salt = secrets.token_bytes(32)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def record_frame(self, frame: WsFrame) -> None:
        decoded_sync = unpack_sync_payloads(frame)
        self._append(
            {
                "kind": "frame",
                "captured_at": datetime.now(UTC).isoformat(),
                "account": _fingerprint(self.account_id, "account", salt=self._salt),
                "payload": redact_structure(
                    frame.model_dump(mode="json", by_alias=True), salt=self._salt
                ),
                "decoded_sync": (
                    redact_structure(decoded_sync, salt=self._salt) if decoded_sync else None
                ),
            }
        )
        self.counts.frames += 1

    async def record_event(self, event: EventEnvelope) -> None:
        payload = event.model_dump(mode="json") if isinstance(event, BaseModel) else event
        self._append(
            {
                "kind": "event",
                "event_type": type(event).__name__,
                "captured_at": datetime.now(UTC).isoformat(),
                "account": _fingerprint(self.account_id, "account", salt=self._salt),
                "payload": redact_structure(payload, salt=self._salt),
            }
        )
        self.counts.events += 1

    async def record_state(self, state: ConnectionStateChanged) -> None:
        self._append(
            {
                "kind": "state",
                "captured_at": datetime.now(UTC).isoformat(),
                "account": _fingerprint(self.account_id, "account", salt=self._salt),
                "state": state.state.value,
                "detail": redact_structure(state.detail, key="detail", salt=self._salt),
            }
        )

    async def record_error(self, error: ErrorOccurred) -> None:
        self._append(
            {
                "kind": "error",
                "captured_at": datetime.now(UTC).isoformat(),
                "account": _fingerprint(self.account_id, "account", salt=self._salt),
                "code": error.code,
                "message": redact_structure(error.message, key="message", salt=self._salt),
            }
        )

    def record_summary(self, summary: dict[str, Any]) -> None:
        """Append safe aggregate counts and field names for acceptance review."""
        self._append(
            {
                "kind": "summary",
                "captured_at": datetime.now(UTC).isoformat(),
                "account": _fingerprint(self.account_id, "account", salt=self._salt),
                "summary": summary,
            }
        )

    def _append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def redact_structure(  # noqa: PLR0911
    value: Any, *, key: str = "", salt: bytes | None = None
) -> Any:
    """Preserve keys/types while replacing all non-protocol string values by hashes."""
    if isinstance(value, dict):
        return {
            str(name): redact_structure(item, key=str(name), salt=salt)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(item, key=key, salt=salt) for item in value]
    if isinstance(value, str):
        if value in _SAFE_PROTOCOL_VALUES or key in {"kind", "event_type"}:
            return value
        if key in {"bizTag", "extJson"}:
            with_json = _redact_json_string(value, salt=salt)
            if with_json is not None:
                return with_json
        if "url" in key.lower():
            return _redact_url(value, salt=salt)
        return _fingerprint(value, _value_kind(key), salt=salt)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if key.lower() in {"code", "version"}:
            return value
        return "<timestamp>" if abs(value) >= 100_000_000_000 else "<number>"
    return value


def _value_kind(key: str) -> str:
    lowered = key.lower()
    if "content" in lowered or "title" in lowered or lowered in {"1", "text", "message"}:
        return "text"
    if "url" in lowered:
        return "url"
    if "id" in lowered or lowered in {"2", "3", "10", "mid", "sid"}:
        return "id"
    return "value"


def _fingerprint(value: str, kind: str, *, salt: bytes | None = None) -> str:
    digest = hmac.new(salt or _PROCESS_SALT, value.encode("utf-8"), hashlib.sha256).hexdigest()[:12]
    return f"<{kind}:len={len(value)}:hmac={digest}>"


def _redact_json_string(value: str, *, salt: bytes | None) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, (dict, list)):
        return None
    return {"_encoding": "json", "value": redact_structure(decoded, salt=salt)}


def _redact_url(value: str, *, salt: bytes | None) -> dict[str, Any]:
    parsed = urlparse(value)
    return {
        "_encoding": "url",
        "scheme": parsed.scheme if parsed.scheme in {"http", "https"} else "other",
        "host": _fingerprint(parsed.netloc, "host", salt=salt) if parsed.netloc else None,
        "path_segments": len([part for part in parsed.path.split("/") if part]),
        "query_keys": sorted({name for name, _ in parse_qsl(parsed.query)}),
    }


def verify_capture(path: Path) -> CaptureVerification:
    """Verify a capture artifact without requiring source Cookie or buyer data."""
    records, issues = _read_capture_records(path)
    if not records and issues:
        return CaptureVerification(False, 0, 0, 0, 0, False, (), tuple(issues))

    summary = _extract_summary(records, issues)
    actual = _count_capture_records(records)
    declared = {
        field: _summary_int(summary, field, issues)
        for field in (
            "frames",
            "events",
            "messages",
            "system_notices",
            "target_messages",
            "duplicate_message_events",
            "errors",
        )
    }
    target_reached = summary.get("target_reached") is True
    missing = tuple(str(value) for value in summary.get("missing_message_fields", []) or [])
    _validate_capture_summary(
        summary,
        declared,
        actual,
        target_reached=target_reached,
        missing=missing,
        issues=issues,
    )
    _scan_capture_records(records, issues)
    return CaptureVerification(
        ok=not issues,
        records=len(records),
        frames=actual.frames,
        events=actual.events,
        messages=actual.messages,
        target_reached=target_reached,
        missing_message_fields=missing,
        issues=tuple(issues),
    )


def _read_capture_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    issues: list[str] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                issues.append(f"第 {line_number} 行不是有效 JSON")
                continue
            if not isinstance(record, dict):
                issues.append(f"第 {line_number} 行不是对象")
                continue
            records.append(record)
    except OSError as exc:
        issues.append(f"读取失败:{exc}")
    return records, issues


def _extract_summary(records: list[dict[str, Any]], issues: list[str]) -> dict[str, Any]:
    summaries = [record for record in records if record.get("kind") == "summary"]
    if len(summaries) != 1:
        issues.append(f"summary 数量应为 1,实际 {len(summaries)}")
    summary = summaries[-1].get("summary", {}) if summaries else {}
    if not isinstance(summary, dict):
        issues.append("summary 格式异常")
        summary = {}
    return summary


def _count_capture_records(records: list[dict[str, Any]]) -> _ActualCaptureCounts:
    return _ActualCaptureCounts(
        frames=sum(record.get("kind") == "frame" for record in records),
        events=sum(record.get("kind") == "event" for record in records),
        messages=sum(
            record.get("kind") == "event" and record.get("event_type") == "MessageReceived"
            for record in records
        ),
        system_notices=sum(
            record.get("kind") == "event" and record.get("event_type") == "SystemNotice"
            for record in records
        ),
        errors=sum(record.get("kind") == "error" for record in records),
        connected_states=sum(
            record.get("kind") == "state" and record.get("state") == "connected"
            for record in records
        ),
    )


def _summary_int(summary: dict[str, Any], field: str, issues: list[str]) -> int:
    try:
        return int(summary.get(field) or 0)
    except (TypeError, ValueError):
        issues.append(f"summary.{field} 不是整数")
        return 0


def _validate_capture_summary(
    summary: dict[str, Any],
    declared: dict[str, int],
    actual: _ActualCaptureCounts,
    *,
    target_reached: bool,
    missing: tuple[str, ...],
    issues: list[str],
) -> None:
    _validate_summary_shape(summary, declared, missing, issues)
    _validate_summary_acceptance(declared, target_reached, missing, issues)
    _validate_summary_counts(summary, declared, actual, target_reached, issues)


def _validate_summary_shape(
    summary: dict[str, Any],
    declared: dict[str, int],
    missing: tuple[str, ...],
    issues: list[str],
) -> None:
    unexpected_fields = set(summary) - _SUMMARY_FIELDS
    if unexpected_fields:
        issues.append("summary 包含未知字段")
    unexpected_missing = set(missing) - _MESSAGE_REQUIRED_FIELDS
    if unexpected_missing:
        issues.append("missing_message_fields 包含未知字段")
    for field, value in declared.items():
        if value < 0:
            issues.append(f"summary.{field} 不能为负数")


def _validate_summary_acceptance(
    declared: dict[str, int],
    target_reached: bool,
    missing: tuple[str, ...],
    issues: list[str],
) -> None:
    if not target_reached:
        issues.append("未达到目标消息数")
    if missing:
        issues.append(f"消息字段缺失:{','.join(missing)}")
    if declared["duplicate_message_events"] > 0:
        issues.append("捕获到重复消息事件")
    if declared["errors"] > 0:
        issues.append("捕获期间存在错误")


def _validate_summary_counts(
    summary: dict[str, Any],
    declared: dict[str, int],
    actual: _ActualCaptureCounts,
    target_reached: bool,
    issues: list[str],
) -> None:
    if summary.get("connected") is not True:
        issues.append("未确认 connected")
    for field in ("frames", "events"):
        actual_value = getattr(actual, field)
        if declared[field] != actual_value:
            issues.append(f"summary.{field}={declared[field]},实际记录={actual_value}")
    if declared["messages"] != actual.messages:
        issues.append(
            f"summary.messages={declared['messages']},实际 MessageReceived={actual.messages}"
        )
    if declared["system_notices"] != actual.system_notices:
        issues.append(
            "summary.system_notices="
            f"{declared['system_notices']},实际 SystemNotice={actual.system_notices}"
        )
    if declared["errors"] != actual.errors:
        issues.append(f"summary.errors={declared['errors']},实际 error={actual.errors}")
    if (summary.get("connected") is True) != (actual.connected_states > 0):
        issues.append(
            f"summary.connected={summary.get('connected') is True},"
            f"实际 connected 状态={actual.connected_states}"
        )
    expected_target_reached = (
        declared["target_messages"] == 0
        or declared["messages"] >= declared["target_messages"]
    )
    if target_reached != expected_target_reached:
        issues.append("target_reached 与目标消息数量矛盾")


def _scan_capture_records(records: list[dict[str, Any]], issues: list[str]) -> None:
    for record in records:
        account = record.get("account")
        if not isinstance(account, str) or not _is_redacted_string(account):
            issues.append("顶层 account 未脱敏")
        for field in ("payload", "decoded_sync"):
            if field in record:
                _scan_redacted_payload(record[field], issues, path=field)
        if record.get("kind") == "error":
            _scan_redacted_payload(record.get("message"), issues, path="error.message")
        if record.get("kind") == "state":
            _scan_redacted_payload(record.get("detail"), issues, path="state.detail")


def _scan_redacted_payload(value: Any, issues: list[str], *, path: str) -> None:
    if isinstance(value, dict):
        for name, item in value.items():
            if name == "query_keys" and isinstance(item, list):
                continue
            _scan_redacted_payload(item, issues, path=f"{path}.{name}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_redacted_payload(item, issues, path=f"{path}[{index}]")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if not isinstance(value, str):
        issues.append(f"{path} 包含不支持的值类型")
        return
    allowed_structural = {
        "json",
        "url",
        "http",
        "https",
        "other",
        "<timestamp>",
        "<number>",
    }
    if value not in _SAFE_PROTOCOL_VALUES | allowed_structural and not _is_redacted_string(value):
        issues.append(f"{path} 包含未脱敏字符串")


def _is_redacted_string(value: str) -> bool:
    return value.startswith("<") and value.endswith(">") and ":hmac=" in value
