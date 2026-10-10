"""Tests for silent long-login renewal (reference-derived, not yet calibrated)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from xianyu_agent.protocol.renew import (
    MODE_COOKIE3,
    MODE_HAVANA,
    QUERY_LOGIN_SETTINGS_URL,
    SET_LOGIN_SETTINGS_URL,
    SILENT_HAS_LOGIN_URL,
    SKIP_FATIGUE,
    SKIP_LONG_LOGIN_EXPIRED,
    RenewOutcome,
    XianyuLongLoginRenewer,
    build_renew_headers,
    build_silent_renew_params,
    changed_cookie_names,
    cookie_time_after,
    is_renew_success,
    javascript_millis,
    merge_set_cookies,
    parse_cookie_values,
    select_renew_mode,
    strict_cookie_time_after,
)

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
FUTURE_MS = int((NOW + timedelta(days=30)).timestamp() * 1000)
PAST_MS = int((NOW - timedelta(days=1)).timestamp() * 1000)


def _cookie(**overrides: object) -> str:
    values: dict[str, object] = {
        "unb": "1",
        "havana_lgc_exp": FUTURE_MS,
        "sdkSilent": PAST_MS,
    }
    values.update(overrides)
    return "; ".join(f"{name}={value}" for name, value in values.items())


def test_cookie_parsing_matches_the_plugin_semantics() -> None:
    parsed = parse_cookie_values("a=1; b=2=3; c; a=9")

    assert parsed["a"] == "9"
    assert parsed["b"] == "2"
    assert parsed["c"] == ""
    assert parse_cookie_values("") == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (str(FUTURE_MS), FUTURE_MS),
        (" 12345 ", 12345.0),
        ("0x10", 16.0),
        ("1_000", math.nan),
        ("Inf", math.nan),
        ("abc", math.nan),
        ("", math.nan),
        (None, math.nan),
        ("99999999999999999", math.nan),
    ],
)
def test_javascript_millis_matches_number_semantics(raw: str | None, expected: float) -> None:
    result = javascript_millis(raw)

    if math.isnan(expected):
        assert math.isnan(result)
    else:
        assert result == expected


def test_cookie_time_after_distinguishes_missing_from_unparsable() -> None:
    assert cookie_time_after(str(FUTURE_MS), NOW) is True
    assert cookie_time_after(str(PAST_MS), NOW) is False
    assert cookie_time_after("garbage", NOW) is True
    assert cookie_time_after("", NOW) is False
    assert cookie_time_after(None, NOW) is False
    assert strict_cookie_time_after("garbage", NOW) is False
    assert strict_cookie_time_after(str(FUTURE_MS), NOW) is True


def test_mode_selection_chooses_havana_then_cookie3_then_expired() -> None:
    havana, reason = select_renew_mode(parse_cookie_values(_cookie()), now=NOW)
    cookie3, _ = select_renew_mode(
        parse_cookie_values(_cookie(havana_lgc_exp=PAST_MS, cookie3_bak_exp=FUTURE_MS)), now=NOW
    )
    expired, expired_reason = select_renew_mode(
        parse_cookie_values(_cookie(havana_lgc_exp=PAST_MS)), now=NOW
    )

    assert (havana, reason) == (MODE_HAVANA, None)
    assert cookie3 == MODE_COOKIE3
    assert (expired, expired_reason) == (None, SKIP_LONG_LOGIN_EXPIRED)


def test_fatigue_window_skips_the_call_unless_the_session_expired() -> None:
    cookies = parse_cookie_values(_cookie(sdkSilent=FUTURE_MS))

    assert select_renew_mode(cookies, now=NOW)[1] == SKIP_FATIGUE
    assert select_renew_mode(cookies, now=NOW, session_expired=True)[0] == MODE_HAVANA


def test_params_and_headers_match_the_observed_shape() -> None:
    havana = build_silent_renew_params(MODE_HAVANA)
    cookie3 = build_silent_renew_params(MODE_COOKIE3)
    headers = build_renew_headers("a=1\nb=2")

    assert havana == [
        ("documentReferer", "https://www.goofish.com/im"),
        ("appName", "xianyu"),
        ("appEntrance", "xianyu_sdkSilent"),
        ("fromSite", "0"),
        ("ltl", "true"),
    ]
    assert cookie3[-2:] == [("skipSessionFilter", "true"), ("c2r", "true")]
    assert headers["Origin"] == "https://www.goofish.com"
    assert headers["Referer"] == "https://www.goofish.com/im"
    assert headers["Cookie"] == "a=1b=2"
    assert "sec-ch-ua" not in headers
    with pytest.raises(ValueError, match="未知静默续期模式"):
        build_silent_renew_params("nope")


def test_success_requires_finished_and_result_code() -> None:
    def body(content: object) -> dict:
        return {"content": content}

    assert is_renew_success(body({"data": {"processFinished": True, "resultCode": 100}})) is True
    assert (
        is_renew_success(
            {"data": {"content": {"data": {"processFinished": True, "resultCode": 100}}}}
        )
        is True
    )
    assert is_renew_success(body({"data": {"processFinished": False, "resultCode": 100}})) is False
    assert is_renew_success(body({"data": {"processFinished": True, "resultCode": 200}})) is False
    assert is_renew_success(body({"data": {"processFinished": True, "resultCode": "100"}})) is True
    assert is_renew_success({}) is False


def test_cookie_merge_prefers_new_values_and_reports_changed_names() -> None:
    merged = merge_set_cookies(
        "unb=1; cookie2=old",
        ["cookie2=new; Path=/; HttpOnly", "havana_lgc_exp=123", "broken"],
    )

    assert merged == "unb=1; cookie2=new; havana_lgc_exp=123"
    assert changed_cookie_names("unb=1; cookie2=old", merged) == ("cookie2", "havana_lgc_exp")
    assert changed_cookie_names("unb=1", "unb=1") == ()


@pytest.mark.asyncio
async def test_renew_posts_the_silent_request_and_merges_cookies() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=SILENT_HAS_LOGIN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"content": {"data": {"processFinished": True, "resultCode": 100}}},
            headers={"set-cookie": "cookie2=refreshed; Path=/"},
        )
    )
    renewer = XianyuLongLoginRenewer(url=SILENT_HAS_LOGIN_URL)

    with router:
        outcome = await renewer.renew(_cookie(), now=NOW)
        request = route.calls[0].request

    assert outcome.success is True
    assert outcome.mode == MODE_HAVANA
    assert outcome.changed_names == ("cookie2",)
    assert "cookie2=refreshed" in outcome.cookie
    assert "ltl=true" in str(request.url)
    assert request.headers["referer"] == "https://www.goofish.com/im"


@pytest.mark.asyncio
async def test_default_renewal_uses_long_login_settings_flow() -> None:
    router = respx.mock(assert_all_called=True)
    set_route = router.post(url__startswith=SET_LOGIN_SETTINGS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"success": True},
            headers={"set-cookie": "cookie2=refreshed; Path=/"},
        )
    )
    query_route = router.post(url__startswith=QUERY_LOGIN_SETTINGS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"returnValue": {"hasLongTokenLogin": True}}},
            headers={"set-cookie": "havana_lgc_exp=next; Path=/"},
        )
    )

    with router:
        outcome = await XianyuLongLoginRenewer().renew(_cookie(), now=NOW)
        set_request = set_route.calls[0].request
        query_request = query_route.calls[0].request

    assert outcome.success is True
    assert outcome.mode == MODE_HAVANA
    assert outcome.changed_names == ("cookie2", "havana_lgc_exp")
    assert "cookie2=refreshed" in outcome.cookie
    assert "havana_lgc_exp=next" in outcome.cookie
    assert "status=0" in str(set_request.content)
    assert "cookie2=refreshed" in query_request.headers["Cookie"]


@pytest.mark.asyncio
async def test_default_renewal_stops_when_setting_long_login_fails() -> None:
    router = respx.mock(assert_all_called=False)
    set_route = router.post(url__startswith=SET_LOGIN_SETTINGS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"success": False},
            headers={"set-cookie": "cookie2=partial; Path=/"},
        )
    )
    query_route = router.post(url__startswith=QUERY_LOGIN_SETTINGS_URL).mock(
        return_value=httpx.Response(200, json={"returnValue": {"hasLongTokenLogin": True}})
    )

    with router:
        outcome = await XianyuLongLoginRenewer().renew(_cookie(), now=NOW)
        set_called = set_route.called
        query_called = query_route.called

    assert outcome.success is False
    assert outcome.need_human_login is True
    assert outcome.changed_names == ("cookie2",)
    assert set_called
    assert not query_called


@pytest.mark.asyncio
async def test_renew_skips_without_calling_and_reports_human_login_needs() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=SILENT_HAS_LOGIN_URL).mock(
        return_value=httpx.Response(200, json={})
    )
    renewer = XianyuLongLoginRenewer(url=SILENT_HAS_LOGIN_URL)

    with router:
        fatigue = await renewer.renew(_cookie(sdkSilent=FUTURE_MS), now=NOW)
        expired = await renewer.renew(_cookie(havana_lgc_exp=PAST_MS), now=NOW)
        empty = await renewer.renew("   ", now=NOW)
        assert not router.calls

    assert fatigue.skipped is True
    assert fatigue.skip_reason == SKIP_FATIGUE
    assert fatigue.need_human_login is False
    assert expired.need_human_login is True
    assert expired.skip_reason == SKIP_LONG_LOGIN_EXPIRED
    assert empty.need_human_login is True


@pytest.mark.asyncio
async def test_renew_reports_unfinished_and_transport_failures() -> None:
    renewer = XianyuLongLoginRenewer(url=SILENT_HAS_LOGIN_URL)
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=SILENT_HAS_LOGIN_URL).mock(
            return_value=httpx.Response(
                200, json={"content": {"data": {"processFinished": False, "resultCode": 100}}}
            )
        )
        unfinished = await renewer.renew(_cookie(), now=NOW)

    assert unfinished.success is False
    assert unfinished.need_human_login is True

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=SILENT_HAS_LOGIN_URL).mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(Exception, match="网络请求失败"):
            await renewer.renew(_cookie(), now=NOW)


def test_outcome_defaults_are_inert() -> None:
    outcome = RenewOutcome()

    assert (outcome.success, outcome.skipped, outcome.cookie) == (False, False, "")
    assert json.dumps({"changed": list(outcome.changed_names)}) == '{"changed": []}'
