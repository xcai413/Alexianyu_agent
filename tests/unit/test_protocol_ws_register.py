"""Regression tests for the M5 WebSocket registration builder split."""

from datetime import UTC, datetime

from xianyu_agent.protocol import ws_auth
from xianyu_agent.protocol.ws import register


def _credentials() -> ws_auth.WsCredentials:
    return ws_auth.WsCredentials(
        access_token="access-token",
        device_id="device-id",
        user_id="user-id",
        expires_at=datetime.now(UTC),
    )


def test_build_registration_frame_preserves_wire_contract(monkeypatch) -> None:
    monkeypatch.setattr(register, "generate_mid", lambda: "mid-1")

    frame = register.build_registration_frame(
        _credentials(),
        app_key=ws_auth.IM_APP_KEY,
        user_agent=ws_auth._BROWSER_HEADERS["User-Agent"],
    )

    assert frame == {
        "lwp": "/reg",
        "headers": {
            "cache-header": "app-key token ua wv",
            "app-key": ws_auth.IM_APP_KEY,
            "token": "access-token",
            "ua": ws_auth._BROWSER_HEADERS["User-Agent"],
            "dt": "j",
            "wv": "im:3,au:3,sy:6",
            "sync": "0,0;0;0;",
            "did": "device-id",
            "mid": "mid-1",
        },
    }


def test_ws_auth_registration_wrapper_preserves_legacy_mid_hook(monkeypatch) -> None:
    monkeypatch.setattr(ws_auth, "_generate_mid", lambda: "mid-2")

    frame = ws_auth.build_registration_frame(_credentials())

    assert frame["headers"]["mid"] == "mid-2"
    assert ws_auth.ws_register is register


def test_ws_auth_registration_mid_alias_starts_canonical() -> None:
    assert ws_auth._generate_mid is register.generate_mid
