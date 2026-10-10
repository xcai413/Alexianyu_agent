"""通知渠道的领域模型与载荷构造。"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from xianyu_agent.domain.notify import (
    ChannelError,
    NotificationChannel,
    NotifyMessage,
    build_request,
    event_label,
    render_message,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _channel(**overrides: object) -> NotificationChannel:
    payload: dict[str, object] = {
        "name": "运维群",
        "kind": "webhook",
        "target": "https://hooks.example.com/robot/send?access_token=abcdef123456",
    }
    payload.update(overrides)
    return NotificationChannel(**payload)  # type: ignore[arg-type]


def test_target_must_be_https_and_not_empty() -> None:
    with pytest.raises(ChannelError, match="只允许 https"):
        _channel(target="http://hooks.example.com/x")
    with pytest.raises(ChannelError, match="主机名"):
        _channel(target="https:///x")
    with pytest.raises(ChannelError, match="不能为空"):
        _channel(target="   ")


def test_kind_and_events_are_whitelisted() -> None:
    with pytest.raises(ChannelError, match="不支持的通知渠道类型"):
        _channel(kind="slack")
    with pytest.raises(ChannelError, match="未知的通知事件"):
        _channel(events=("delivery_job", "something_else"))


def test_telegram_requires_secret_and_takes_chat_id_as_target() -> None:
    with pytest.raises(ChannelError, match="必须提供 SecretVault 引用"):
        _channel(kind="telegram", target="12345")
    channel = _channel(kind="telegram", target="12345", secret_ref="bot-token")
    assert channel.target == "12345"


def test_mask_hides_path_and_non_url_target() -> None:
    channel = _channel()
    assert channel.masked_target() == "https://hooks.example.com/…"
    assert "access_token" not in channel.masked_target()
    telegram = _channel(kind="telegram", target="1234567890", secret_ref="ref")
    assert telegram.masked_target() == "1234…"


def test_events_default_to_everything_and_dedupe_keeps_order() -> None:
    assert _channel().accepts("account_risk")
    channel = _channel(events=("account_risk", "delivery_job", "account_risk"))
    assert channel.events == ("account_risk", "delivery_job")
    assert channel.accepts("account_risk")
    assert not channel.accepts("automation_run")


def test_event_label_falls_back_to_raw_value() -> None:
    assert event_label("delivery_job") == "发货任务"
    assert event_label("whatever") == "whatever"


def test_render_message_contains_only_operational_fields() -> None:
    text = render_message(
        NotifyMessage(
            event="delivery_job",
            title="发货任务需要人工处理",
            body="原因:平台结果不明\n收尾命令:xianyu-agent automation resolve 1 --reason 已确认",
            level="uncertain",
            account_id="test-account",
            target="test-order-001",
        )
    )
    assert "账号:test-account" in text
    assert "事件:发货任务" in text
    assert "test-order-001" in text


def test_webhook_payload_is_plain_json() -> None:
    request = build_request(
        _channel(),
        NotifyMessage(
            event="account_risk", title="告警", body="需要验证", level="needs_validation"
        ),
        now=NOW,
    )
    assert request.method == "POST"
    assert request.url.endswith("access_token=abcdef123456")
    assert request.payload["event"] == "account_risk"


def test_dingtalk_signature_matches_documented_algorithm() -> None:
    request = build_request(
        _channel(kind="dingtalk"),
        NotifyMessage(event="account_risk", title="告警", body="需要验证"),
        secret="SEC123",
        now=NOW,
    )
    query = parse_qs(urlsplit(request.url).query)
    timestamp = query["timestamp"][0]
    string_to_sign = f"{timestamp}\nSEC123"
    expected = base64.b64encode(
        hmac.new(b"SEC123", string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    assert query["sign"][0] == expected
    assert request.payload["msgtype"] == "markdown"


def test_dingtalk_without_secret_sends_unsigned() -> None:
    request = build_request(
        _channel(kind="dingtalk"),
        NotifyMessage(event="account_risk", title="告警", body="需要验证"),
        now=NOW,
    )
    assert "sign=" not in request.url


def test_feishu_signature_uses_empty_message_body() -> None:
    request = build_request(
        _channel(kind="feishu"),
        NotifyMessage(event="account_risk", title="告警", body="需要验证"),
        secret="FEISHU",
        now=NOW,
    )
    timestamp = request.payload["timestamp"]
    string_to_sign = f"{timestamp}\nFEISHU"
    expected = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    ).decode("utf-8")
    assert request.payload["sign"] == expected
    assert request.payload["msg_type"] == "text"


def test_bark_posts_device_key_to_push_endpoint() -> None:
    request = build_request(
        _channel(kind="bark", target="https://api.day.app", secret_ref="bark-key"),
        NotifyMessage(event="account_risk", title="告警", body="需要验证"),
        secret="DEVICEKEY",
        now=NOW,
    )
    assert request.url == "https://api.day.app/push"
    assert request.payload["device_key"] == "DEVICEKEY"


def test_bark_without_resolved_secret_is_a_config_error() -> None:
    with pytest.raises(ChannelError, match="设备密钥"):
        build_request(
            _channel(kind="bark", target="https://api.day.app", secret_ref="bark-key"),
            NotifyMessage(event="account_risk", title="告警", body="需要验证"),
            now=NOW,
        )


def test_telegram_builds_bot_url_and_never_logs_token() -> None:
    request = build_request(
        _channel(kind="telegram", target="12345", secret_ref="bot-token"),
        NotifyMessage(event="account_risk", title="告警", body="需要验证"),
        secret="BOTTOKEN",
        now=NOW,
    )
    assert request.url == "https://api.telegram.org/botBOTTOKEN/sendMessage"
    assert request.payload["chat_id"] == "12345"
