"""卖家资料只读接口:接线形状、字段解析与失败分类。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.mtop.endpoints.user_profile import (
    USER_PAGE_NAV_API,
    USER_PAGE_NAV_SPM,
    USER_PAGE_NAV_URL,
    UserProfile,
    UserProfileError,
    XianyuUserProfileClient,
    build_user_profile_params,
    parse_user_profile,
)
from xianyu_agent.protocol.mtop.signer import APP_KEY, compute_sign

COOKIE = "unb=1; _m_h5_tk=abc123_1699999999999; _m_h5_tk_enc=xyz"
runner = CliRunner()


class _Signer:
    def __init__(self, cookie: str = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie


@pytest.fixture
async def profile_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "profile.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def test_profile_params_match_the_reference_extras() -> None:
    params = build_user_profile_params("token", "{}", timestamp="1699999999999")

    assert params["api"] == USER_PAGE_NAV_API
    assert params["v"] == "1.0"
    assert params["ecode"] == "0"
    assert params["spm_cnt"] == USER_PAGE_NAV_SPM
    assert params["sign"]


def test_profile_signature_uses_token_seed_without_expiry_suffix() -> None:
    params = build_user_profile_params("abc123_1699999999999", "{}", timestamp="1699999999999")
    assert params["sign"] == compute_sign("abc123", 1699999999999, APP_KEY, "{}")


def test_parse_profile_prefers_display_name_and_falls_back() -> None:
    primary = parse_user_profile(
        {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "module": {
                    "base": {
                        "displayName": "主昵称",
                        "displayNick": "次昵称",
                        "avatar": "https://img.example/a.png",
                    }
                }
            },
        }
    )
    fallback = parse_user_profile(
        {
            "ret": ["SUCCESS::调用成功"],
            "data": {"module": {"base": {"displayNick": "只有次昵称"}}},
        }
    )

    assert primary.nickname == "主昵称"
    assert primary.avatar_url == "https://img.example/a.png"
    assert fallback.nickname == "只有次昵称"
    assert fallback.avatar_url is None


def test_parse_profile_tolerates_a_missing_payload_shape() -> None:
    profile = parse_user_profile({"ret": ["SUCCESS::调用成功"], "data": {}})

    assert profile.nickname is None


def test_parse_profile_raises_on_a_business_error() -> None:
    with pytest.raises(UserProfileError, match="读取账号资料失败"):
        parse_user_profile({"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})


@pytest.mark.asyncio
@respx.mock
async def test_fetch_profile_posts_a_signed_empty_body() -> None:
    route = respx.post(url__startswith=USER_PAGE_NAV_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": ["SUCCESS::调用成功"],
                "data": {"module": {"base": {"displayName": "卖家昵称"}}},
            },
        )
    )
    client = XianyuUserProfileClient(_Signer())  # type: ignore[arg-type]

    profile = await client.fetch_profile("acc-1")

    request = route.calls.last.request
    assert profile.nickname == "卖家昵称"
    assert "api=" + USER_PAGE_NAV_API in str(request.url)
    assert request.content.decode() == "data=%7B%7D"
    assert request.headers["Referer"] == "https://www.goofish.com/"


@pytest.mark.asyncio
async def test_fetch_profile_fails_closed_without_credentials() -> None:
    with pytest.raises(UserProfileError, match="无可用 Cookie"):
        await XianyuUserProfileClient(_Signer("")).fetch_profile("acc-1")  # type: ignore[arg-type]
    with pytest.raises(UserProfileError, match="_m_h5_tk"):
        await XianyuUserProfileClient(_Signer("unb=1")).fetch_profile(  # type: ignore[arg-type]
            "acc-1"
        )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_profile_reports_http_failures() -> None:
    respx.post(url__startswith=USER_PAGE_NAV_URL).mock(return_value=httpx.Response(503))
    client = XianyuUserProfileClient(_Signer())  # type: ignore[arg-type]

    with pytest.raises(UserProfileError, match="HTTP 状态异常"):
        await client.fetch_profile("acc-1")


def test_cli_refresh_profile_stores_the_nickname(
    profile_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def seed() -> None:
        await domain_accounts.create_account("acc-1", enabled=True)

    asyncio.run(seed())
    module = __import__("xianyu_agent.cli.commands.account", fromlist=["XianyuUserProfileClient"])

    class _Client:
        async def fetch_profile(self, _account_id: str):
            return UserProfile(nickname="平台昵称", avatar_url="https://img.example/a.png")

    monkeypatch.setattr(module, "XianyuUserProfileClient", _Client)
    result = runner.invoke(app, ["account", "refresh-profile", "-i", "acc-1"])

    assert result.exit_code == 0, result.output
    assert "平台昵称" in result.output
    record = asyncio.run(domain_accounts.get_account("acc-1"))
    assert record is not None
    assert record.nickname == "平台昵称"


def test_cli_reports_unknown_account(profile_db: None) -> None:
    result = runner.invoke(app, ["account", "refresh-profile", "-i", "missing"])

    assert result.exit_code == 1
    assert "不存在" in result.output
