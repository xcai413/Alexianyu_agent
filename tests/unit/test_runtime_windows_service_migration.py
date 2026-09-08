"""Regression tests for the Phase 1 Windows platform migration."""

from pathlib import Path

from xianyu_agent.runtime import service_runner, watchdog
from xianyu_agent.runtime.platform import windows_service as runtime_windows_service
from xianyu_agent.services import windows_service as legacy_windows_service


def test_legacy_windows_service_is_canonical_module() -> None:
    assert legacy_windows_service is runtime_windows_service


def test_default_project_root_depth_is_preserved_after_move() -> None:
    expected_root = Path(runtime_windows_service.__file__).resolve().parents[4]
    assert runtime_windows_service.resolve_task_paths().project_root == expected_root


def test_runtime_callers_use_canonical_windows_platform_functions() -> None:
    assert service_runner.is_service_paused is runtime_windows_service.is_service_paused
    assert watchdog.is_service_paused is runtime_windows_service.is_service_paused
    assert watchdog.end_task is runtime_windows_service.end_task
    assert watchdog.start_task is runtime_windows_service.start_task
    assert watchdog.WindowsServiceError is runtime_windows_service.WindowsServiceError
