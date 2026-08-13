"""P0.3 Windows 任务计划 XML、命令与 CLI 测试。"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from xianyu_agent.cli.commands import service as service_cli
from xianyu_agent.cli.main import app as cli_app
from xianyu_agent.services import windows_service
from xianyu_agent.services.daemon_health import DaemonHealth
from xianyu_agent.services.windows_service import (
    SYSTEM_SID,
    TASK_NAME,
    TASK_NAMESPACE,
    WATCHDOG_TASK_NAME,
    ScheduledTaskStatus,
    TaskPaths,
    WindowsServiceError,
    build_task_xml,
    build_watchdog_task_xml,
    format_task_result,
)


def _paths(tmp_path: Path) -> TaskPaths:
    return TaskPaths(
        project_root=tmp_path,
        python_executable=tmp_path / ".venv" / "Scripts" / "python.exe",
        pythonw_executable=tmp_path / ".venv" / "Scripts" / "pythonw.exe",
        xml_path=tmp_path / "data" / "runtime" / "scheduled-task.xml",
        watchdog_xml_path=tmp_path / "data" / "runtime" / "watchdog-task.xml",
    )


def _find(root: ET.Element, path: str) -> ET.Element:
    row = root.find(path, {"t": TASK_NAMESPACE})
    assert row is not None
    return row


def test_user_task_xml_contains_persistence_and_safety_settings(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    xml = build_task_xml(paths, startup="user", user_sid="S-1-5-21-1000")
    root = ET.fromstring(xml)
    assert _find(root, "t:Triggers/t:LogonTrigger/t:UserId").text == "S-1-5-21-1000"
    assert _find(root, "t:Principals/t:Principal/t:LogonType").text == "InteractiveToken"
    assert _find(root, "t:Settings/t:Hidden").text == "true"
    assert _find(root, "t:Settings/t:ExecutionTimeLimit").text == "PT0S"
    assert _find(root, "t:Settings/t:MultipleInstancesPolicy").text == "IgnoreNew"
    assert _find(root, "t:Settings/t:RestartOnFailure/t:Interval").text == "PT1M"
    assert _find(root, "t:Settings/t:RestartOnFailure/t:Count").text == "10"
    assert _find(root, "t:Actions/t:Exec/t:Command").text == str(paths.pythonw_executable)
    assert (
        _find(root, "t:Actions/t:Exec/t:Arguments").text
        == "-m xianyu_agent.services.service_runner"
    )
    assert _find(root, "t:Actions/t:Exec/t:WorkingDirectory").text == str(tmp_path)
    assert "FERNET" not in xml
    assert "Cookie" not in xml


def test_system_task_xml_uses_boot_trigger_and_service_account(tmp_path: Path) -> None:
    root = ET.fromstring(build_task_xml(_paths(tmp_path), startup="system"))
    assert _find(root, "t:Triggers/t:BootTrigger/t:Delay").text == "PT30S"
    assert _find(root, "t:Principals/t:Principal/t:UserId").text == SYSTEM_SID
    assert _find(root, "t:Principals/t:Principal/t:LogonType").text == "ServiceAccount"
    assert _find(root, "t:Principals/t:Principal/t:RunLevel").text == "HighestAvailable"


def test_watchdog_task_xml_repeats_every_minute(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = ET.fromstring(
        build_watchdog_task_xml(paths, startup="user", user_sid="S-1-5-21-1000")
    )
    assert (
        _find(root, "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval").text
        == "PT1M"
    )
    assert _find(root, "t:Triggers/t:TimeTrigger/t:StartBoundary").text
    assert _find(root, "t:Settings/t:ExecutionTimeLimit").text == "PT1M"
    assert root.find("t:Settings/t:RestartOnFailure", {"t": TASK_NAMESPACE}) is None
    assert (
        _find(root, "t:Actions/t:Exec/t:Arguments").text
        == "-m xianyu_agent.services.watchdog_runner"
    )


def test_task_xml_validates_startup_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported startup"):
        build_task_xml(_paths(tmp_path), startup="invalid")
    with pytest.raises(ValueError, match="requires user_sid"):
        build_task_xml(_paths(tmp_path), startup="user")


def test_format_task_result_labels_running_and_unknown() -> None:
    assert format_task_result(0x00041301) == "running (0x00041301)"
    assert format_task_result(0x800710E0) == "request_refused (0x800710E0)"
    assert format_task_result(123) == "unknown (0x0000007B)"
    assert format_task_result(None) == "-"


def test_install_task_writes_utf16_xml_and_invokes_schtasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(windows_service, "_require_windows", lambda: None)
    monkeypatch.setattr(windows_service, "resolve_task_paths", lambda **_kwargs: paths)
    monkeypatch.setattr(windows_service, "current_user_sid", lambda: "S-1-5-21-1000")

    def fake_run(args: list[str]):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(windows_service, "_run", fake_run)
    result = windows_service.install_task(startup="user", project_root=tmp_path)
    assert result == paths
    assert paths.xml_path.exists()
    assert paths.watchdog_xml_path.exists()
    content = paths.xml_path.read_text(encoding="utf-16")
    assert "S-1-5-21-1000" in content
    assert calls == [
        ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", str(paths.xml_path), "/F"],
        [
            "schtasks.exe",
            "/Create",
            "/TN",
            WATCHDOG_TASK_NAME,
            "/XML",
            str(paths.watchdog_xml_path),
            "/F",
        ],
    ]


def test_query_status_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_service, "_require_windows", lambda: None)
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "not found"),
    )
    assert windows_service.query_task_status() == ScheduledTaskStatus(installed=False)


def test_query_status_parses_powershell_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_service, "_require_windows", lambda: None)
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "exists", ""),
    )
    payload = json.dumps(
        {
            "state": "Running",
            "last_run_time": "2026-08-14T01:00:00+08:00",
            "last_task_result": 0,
            "next_run_time": "0001-01-01T00:00:00",
        }
    )
    monkeypatch.setattr(
        windows_service,
        "_run",
        lambda args: subprocess.CompletedProcess(args, 0, payload, ""),
    )
    row = windows_service.query_task_status()
    assert row.installed is True
    assert row.state == "Running"
    assert row.last_task_result == 0
    assert row.next_run_time is None


def test_query_status_accepts_null_task_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_service, "_require_windows", lambda: None)
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "exists", ""),
    )
    payload = json.dumps(
        {
            "state": "Ready",
            "last_run_time": None,
            "last_task_result": 0,
            "next_run_time": None,
        }
    )
    monkeypatch.setattr(
        windows_service,
        "_run",
        lambda args: subprocess.CompletedProcess(args, 0, payload, ""),
    )
    row = windows_service.query_task_status()
    assert row.installed is True
    assert row.last_run_time is None
    assert row.next_run_time is None


def test_service_cli_install_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paused: list[bool] = []
    monkeypatch.setattr(service_cli, "install_task", lambda startup: paths)
    monkeypatch.setattr(service_cli, "is_service_paused", lambda: False)
    monkeypatch.setattr(
        service_cli,
        "set_service_paused",
        lambda value: paused.append(value) or tmp_path,
    )
    monkeypatch.setattr(
        service_cli,
        "query_task_status",
        lambda _task_name=TASK_NAME: ScheduledTaskStatus(
            installed=True, state="Ready", last_task_result=0
        ),
    )

    async def no_daemon():
        return None

    monkeypatch.setattr(service_cli.daemon_domain, "latest_instance", no_daemon)
    runner = CliRunner()
    install = runner.invoke(cli_app, ["service", "install"])
    assert install.exit_code == 0
    assert TASK_NAME in install.output
    assert "开启暂停门" in install.output
    assert paused == [True]
    status = runner.invoke(cli_app, ["service", "status"])
    assert status.exit_code == 0
    assert "Ready" in status.output


def test_service_status_handles_stopped_daemon_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_cli,
        "query_task_status",
        lambda _task_name=TASK_NAME: ScheduledTaskStatus(
            installed=True, state="Ready", last_task_result=0
        ),
    )

    class StoppedDaemon:
        status = "stopped"
        pid = 12345
        last_heartbeat_at = datetime.now(UTC)

    async def stopped_daemon():
        return StoppedDaemon()

    monkeypatch.setattr(service_cli.daemon_domain, "latest_instance", stopped_daemon)
    result = CliRunner().invoke(cli_app, ["service", "status"])
    assert result.exit_code == 0
    assert "stopped" in result.output


def test_service_cli_surfaces_windows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise WindowsServiceError("access denied")

    monkeypatch.setattr(service_cli, "install_task", fail)
    monkeypatch.setattr(service_cli, "is_service_paused", lambda: False)
    paused: list[bool] = []
    monkeypatch.setattr(service_cli, "set_service_paused", paused.append)
    result = CliRunner().invoke(cli_app, ["service", "install"])
    assert result.exit_code == 1
    assert "access denied" in result.output
    assert paused == [True, False]


@pytest.mark.asyncio
async def test_wait_offline_requires_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def latest():
        return object()

    monkeypatch.setattr(service_cli.daemon_domain, "latest_instance", latest)
    monkeypatch.setattr(
        service_cli,
        "observe_daemon",
        lambda _row: DaemonHealth("stale", True, True, 120.0, False),
    )
    assert await service_cli._wait_daemon(online=False, timeout_s=0.01) is False
