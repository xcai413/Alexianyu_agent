"""Web 容器不能因只有 HTTP 存活而在认证或撤销存储失效时报告健康。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from sqlalchemy.exc import OperationalError


@pytest.fixture
def healthcheck(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "docker" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("container_healthcheck", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("XIANYU_CONTAINER_SERVICE", "web")
    return module


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_web_health_requires_http_and_revocation_database(healthcheck, monkeypatch):
    checked = []

    def open_url(url, timeout):
        assert url == "http://127.0.0.1:8090/api/v1/web/auth/session"
        assert timeout == 5
        return Response({"data": {"authenticated": False}})

    async def database():
        checked.append("database")

    monkeypatch.setattr(healthcheck, "urlopen", open_url)
    monkeypatch.setattr(healthcheck, "_check_web_database", database)
    assert healthcheck.main() == 0
    assert checked == ["database"]


def test_unconfigured_web_auth_is_unhealthy(healthcheck, monkeypatch):
    def unavailable(*_, **__):
        raise HTTPError("http://test", 503, "WEB_AUTH_UNCONFIGURED", {}, None)

    monkeypatch.setattr(healthcheck, "urlopen", unavailable)
    assert healthcheck.main() == 1


def test_missing_revocation_storage_is_unhealthy_without_secret_output(
    healthcheck, monkeypatch, capsys
):
    monkeypatch.setattr(
        healthcheck, "urlopen", lambda *_, **__: Response({"data": {"authenticated": False}})
    )

    async def unavailable():
        raise OperationalError("sensitive connection details", {}, RuntimeError("unavailable"))

    monkeypatch.setattr(healthcheck, "_check_web_database", unavailable)
    assert healthcheck.main() == 1
    output = capsys.readouterr().err
    assert "OperationalError" in output
    assert "sensitive" not in output


@pytest.mark.parametrize("payload", [{}, {"data": {"authenticated": True}}])
def test_invalid_session_response_is_unhealthy(healthcheck, monkeypatch, payload):
    monkeypatch.setattr(healthcheck, "urlopen", lambda *_, **__: Response(payload))
    assert healthcheck.main() == 1


def test_daemon_health_keeps_its_own_endpoint(healthcheck, monkeypatch):
    monkeypatch.delenv("XIANYU_CONTAINER_SERVICE")

    def open_url(url, timeout):
        assert url.endswith(":8089/health")
        return Response({"status": "healthy"})

    monkeypatch.setattr(healthcheck, "urlopen", open_url)
    assert healthcheck.main() == 0
