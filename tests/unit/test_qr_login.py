"""Unit tests for QR login flow (passport endpoints mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

import xianyu_agent.protocol.qr_login as qr_mod
from xianyu_agent.cli.commands.auth import _print_qr_ascii
from xianyu_agent.protocol.qr_login import (
    API_GENERATE_QR,
    API_MINI_LOGIN,
    API_SCAN_STATUS,
    H5API_INDEX,
    QRLoginClient,
    QrLoginError,
    QrStatus,
)

MINI_LOGIN_HTML = (
    "<html><script>window.viewData = "
    '{"loginFormData": {"key1": "v1", "key2": "v2"}};'
    "</script></html>"
)


def test_terminal_qr_preview_is_ascii_only(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []

    class FakeQr:
        @staticmethod
        def get_matrix():
            return [[True, False], [False, True]]

    monkeypatch.setattr(
        "xianyu_agent.cli.commands.auth.console.print",
        lambda value, **_kwargs: lines.append(value),
    )
    _print_qr_ascii(FakeQr())
    assert lines == ["##  ", "  ##"]
    assert all(line.isascii() for line in lines)


def _qr_generate_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": {
                "success": True,
                "data": {
                    "t": "T123",
                    "ck": "CK456",
                    "codeContent": "https://qr.example/scan",
                },
            }
        },
    )


@pytest.fixture
def mock_passport():
    router = respx.mock(assert_all_called=False)
    router.get(H5API_INDEX).mock(
        return_value=httpx.Response(
            200,
            headers=[
                ("set-cookie", "m_h5_tk=seed_abc; Path=/"),
                ("set-cookie", "cookie2=c2; Path=/"),
            ],
        )
    )
    router.post(H5API_INDEX).mock(return_value=httpx.Response(200, json={}))
    router.get(API_MINI_LOGIN).mock(return_value=httpx.Response(200, text=MINI_LOGIN_HTML))
    router.get(API_GENERATE_QR).mock(return_value=_qr_generate_response())
    with router:
        yield router


@pytest.mark.asyncio
async def test_generate_three_steps(mock_passport) -> None:
    client = QRLoginClient()
    session = await client.generate()
    assert session.status == QrStatus.WAITING
    assert session.qr_content == "https://qr.example/scan"
    assert session.params.get("t") == "T123"
    assert session.params.get("ck") == "CK456"
    assert session.params.get("key1") == "v1"
    assert session.cookies.get("m_h5_tk") == "seed_abc"


@pytest.mark.asyncio
async def test_generate_fails_when_no_viewdata(mock_passport) -> None:
    mock_passport.get(API_MINI_LOGIN).mock(
        return_value=httpx.Response(200, text="<html>no viewData here</html>")
    )
    client = QRLoginClient()
    with pytest.raises(QrLoginError, match="viewData"):
        await client.generate()


@pytest.mark.asyncio
async def test_generate_fails_when_qr_api_error(mock_passport) -> None:
    mock_passport.get(API_GENERATE_QR).mock(
        return_value=httpx.Response(200, json={"content": {"success": False}})
    )
    client = QRLoginClient()
    with pytest.raises(QrLoginError, match="二维码"):
        await client.generate()


@pytest.mark.asyncio
async def test_generate_retries_transient_passport_connect_error(mock_passport) -> None:
    calls = {"count": 0}

    def flaky_params(_request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("temporary")
        return httpx.Response(200, text=MINI_LOGIN_HTML)

    mock_passport.get(API_MINI_LOGIN).mock(side_effect=flaky_params)
    session = await QRLoginClient().generate()
    assert session.qr_content == "https://qr.example/scan"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_generate_reports_stage_after_retry_exhausted(
    mock_passport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qr_mod, "HTTP_RETRY_DELAYS_S", (0.0, 0.0))
    mock_passport.get(API_MINI_LOGIN).mock(side_effect=httpx.ConnectError("temporary"))
    monkeypatch.setattr(
        qr_mod.httpx,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("sync temporary")),
    )
    with pytest.raises(QrLoginError, match=r"登录参数.*ConnectError.*3 次"):
        await QRLoginClient().generate()


@pytest.mark.asyncio
async def test_poll_success_collects_cookies(mock_passport) -> None:
    mock_passport.post(API_SCAN_STATUS).mock(
        return_value=httpx.Response(
            200,
            json={"content": {"data": {"qrCodeStatus": "CONFIRMED"}}},
            headers=[
                ("set-cookie", "unb=2214350705775; Path=/"),
                ("set-cookie", "cookie2=xyz; Path=/"),
            ],
        )
    )
    client = QRLoginClient()
    session = await client.generate()
    status = await client.poll(session)
    assert status == QrStatus.SUCCESS
    assert session.unb == "2214350705775"
    assert "unb=2214350705775" in session.cookie_string()


@pytest.mark.asyncio
async def test_poll_success_carries_client_cookie_jar(mock_passport) -> None:
    mock_passport.post(API_SCAN_STATUS).mock(
        return_value=httpx.Response(
            200,
            json={"content": {"data": {"qrCodeStatus": "CONFIRMED"}}},
            headers=[
                ("set-cookie", "unb=2214350705775; Path=/"),
                ("set-cookie", "session-extra=kept; Path=/"),
            ],
        )
    )
    session = await QRLoginClient().generate()
    assert await QRLoginClient().poll(session) == QrStatus.SUCCESS
    assert session.cookies["session-extra"] == "kept"


@pytest.mark.asyncio
async def test_poll_verification_required(mock_passport) -> None:
    mock_passport.post(API_SCAN_STATUS).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": {
                    "data": {
                        "qrCodeStatus": "CONFIRMED",
                        "iframeRedirect": True,
                        "iframeRedirectUrl": "https://passport.goofish.com/verify",
                    }
                }
            },
        )
    )
    client = QRLoginClient()
    session = await client.generate()
    status = await client.poll(session)
    assert status == QrStatus.VERIFICATION_REQUIRED
    assert session.verification_url == "https://passport.goofish.com/verify"


@pytest.mark.asyncio
async def test_poll_scanned_then_expired(mock_passport) -> None:
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        code = "SCANED" if calls["n"] == 1 else "EXPIRED"
        return httpx.Response(200, json={"content": {"data": {"qrCodeStatus": code}}})

    mock_passport.post(API_SCAN_STATUS).mock(side_effect=handler)
    client = QRLoginClient()
    session = await client.generate()
    assert await client.poll(session) == QrStatus.SCANNED
    assert await client.poll(session) == QrStatus.EXPIRED


@pytest.mark.asyncio
async def test_poll_cancelled(mock_passport) -> None:
    mock_passport.post(API_SCAN_STATUS).mock(
        return_value=httpx.Response(200, json={"content": {"data": {}}})
    )
    client = QRLoginClient()
    session = await client.generate()
    assert await client.poll(session) == QrStatus.CANCELLED


@pytest.mark.asyncio
async def test_wait_for_login_timeout_expires(mock_passport) -> None:
    mock_passport.post(API_SCAN_STATUS).mock(
        return_value=httpx.Response(200, json={"content": {"data": {"qrCodeStatus": "NEW"}}})
    )
    client = QRLoginClient()
    session = await client.generate()
    # 调小轮询间隔成本:直接测超时路径(轮询间隔 0.8s,1.5s 超时 -> 2 次轮询后过期)
    orig = qr_mod.POLL_INTERVAL_S
    qr_mod.POLL_INTERVAL_S = 0.05
    try:
        status = await client.wait_for_login(session, timeout_s=0.12)
    finally:
        qr_mod.POLL_INTERVAL_S = orig
    assert status == QrStatus.EXPIRED
