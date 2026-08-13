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

from xianyu_agent.protocol.events import EventEnvelope, WsFrame
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
    issues: list[str] = []
    records: list[dict[str, Any]] = []
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
        return CaptureVerification(False, 0, 0, 0, 0, False, (), (f"读取失败:{exc}",))

    summaries = [record for record in records if record.get("kind") == "summary"]
    if len(summaries) != 1:
        issues.append(f"summary 数量应为 1,实际 {len(summaries)}")
    summary = summaries[-1].get("summary", {}) if summaries else {}
    if not isinstance(summary, dict):
        issues.append("summary 格式异常")
        summary = {}
    frames = sum(record.get("kind") == "frame" for record in records)
    events = sum(record.get("kind") == "event" for record in records)
    messages = int(summary.get("messages") or 0)
    target_reached = bool(summary.get("target_reached"))
    missing = tuple(str(value) for value in summary.get("missing_message_fields", []) or [])
    if not bool(summary.get("connected")):
        issues.append("未确认 connected")
    if not target_reached:
        issues.append("未达到目标消息数")
    if missing:
        issues.append(f"消息字段缺失:{','.join(missing)}")
    if int(summary.get("duplicate_message_events") or 0) > 0:
        issues.append("捕获到重复消息事件")
    if int(summary.get("errors") or 0) > 0:
        issues.append("捕获期间存在错误")
    _scan_for_plain_sensitive_values(records, issues)
    return CaptureVerification(
        ok=not issues,
        records=len(records),
        frames=frames,
        events=events,
        messages=messages,
        target_reached=target_reached,
        missing_message_fields=missing,
        issues=tuple(issues),
    )


def _scan_for_plain_sensitive_values(value: Any, issues: list[str], *, key: str = "root") -> None:
    if isinstance(value, dict):
        for name, item in value.items():
            _scan_for_plain_sensitive_values(item, issues, key=str(name))
        return
    if isinstance(value, list):
        for item in value:
            _scan_for_plain_sensitive_values(item, issues, key=key)
        return
    if not isinstance(value, str):
        return
    sensitive_key = key.lower() in {
        "account",
        "account_id",
        "chat_id",
        "message_id",
        "item_id",
        "sender_id",
        "sender_name",
        "receiver_id",
        "content",
        "token",
        "cookie",
        "encrypted_token",
        "device_id",
    }
    if sensitive_key and not (
        (value.startswith("<") and value.endswith(">")) or value in _SAFE_PROTOCOL_VALUES
    ):
        issues.append(f"敏感字段 {key} 包含未脱敏字符串")
