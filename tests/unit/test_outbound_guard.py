"""出站守卫(Issue #5 Phase 6, §19/M54)。"""

from __future__ import annotations

import socket

import pytest

from xianyu_agent.infrastructure.security.outbound_guard import (
    OutboundBlocked,
    check_url,
    require_url,
)


def _fake_addrinfo(address: str):
    def _resolver(*_args: object, **_kwargs: object):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

    return _resolver


def test_https_public_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))

    check = check_url("https://cards.example.com/dispatch")

    assert check.allowed is True


def test_non_https_schemes_are_rejected() -> None:
    for url in ("http://cards.example.com/x", "file:///etc/passwd", "ftp://h/x"):
        assert check_url(url).allowed is False


def test_localhost_and_metadata_targets_are_rejected() -> None:
    assert check_url("https://localhost/x").allowed is False
    assert check_url("https://metadata.google.internal/x").allowed is False
    assert check_url("https://127.0.0.1/x").allowed is False
    assert check_url("https://169.254.169.254/latest/meta-data").allowed is False
    assert check_url("https://[::1]/x").allowed is False


def test_private_ranges_and_hostnames_resolving_inside_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert check_url("https://10.0.0.5/x").allowed is False
    assert check_url("https://192.168.1.10/x").allowed is False

    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("127.0.0.1"))
    inside = check_url("https://cards.example.com/x")

    assert inside.allowed is False
    assert "内网地址" in inside.reason


def test_missing_host_and_dns_failure_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_url("https:///path", resolve_dns=False).allowed is False

    def _boom(*_args: object, **_kwargs: object):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    check = check_url("https://cards.example.com/x")

    assert check.allowed is False
    assert "解析失败" in check.reason


def test_explicit_allow_hosts_overrides_blocking() -> None:
    assert check_url("https://localhost:8080/x", allow_hosts=("localhost",)).allowed is True


def test_require_url_raises_with_reason() -> None:
    with pytest.raises(OutboundBlocked, match="不允许出站"):
        require_url("https://127.0.0.1/x")

    require_url("https://cards.example.com/x", resolve_dns=False)
