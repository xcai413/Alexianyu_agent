"""Regression coverage for canonical CLI credential mutation authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import typer

from xianyu_agent.application.session import (
    CredentialFailureCode,
    CredentialHandle,
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
)
from xianyu_agent.cli.commands import auth as auth_cli

ACCOUNT_ID = "cli-credential-account"


class _FakeLock:
    def __init__(self, held: dict[str, bool]) -> None:
        self._held = held
        self._held["value"] = True
        self.release_calls = 0

    def release(self) -> None:
        assert self._held["value"] is True
        self.release_calls += 1
        self._held["value"] = False


class _FakeSupervisor:
    def __init__(
        self,
        held: dict[str, bool],
        *,
        health: CredentialHealth,
        result: CredentialResult[object],
    ) -> None:
        self._held = held
        self.health = health
        self.result = result
        self.inspect_calls: list[str] = []
        self.refresh_calls: list[tuple[str, bool]] = []

    async def inspect(self, account_id: str) -> CredentialHealth:
        assert self._held["value"] is True
        self.inspect_calls.append(account_id)
        return self.health

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[object]:
        assert self._held["value"] is True
        self.refresh_calls.append((account_id, validation_recovery))
        return self.result


def _health(
    state: CredentialHealthState,
    *,
    cooling: bool = False,
) -> CredentialHealth:
    return CredentialHealth(
        account_id=ACCOUNT_ID,
        state=state,
        expires_at=datetime.now(UTC) + timedelta(hours=8)
        if state is CredentialHealthState.HEALTHY
        else None,
        validation_cooling=cooling,
    )


def _result(
    state: CredentialResultState,
    health: CredentialHealth,
    *,
    code: CredentialFailureCode | None = None,
    secret: str | None = None,
) -> CredentialResult[object]:
    return CredentialResult(
        state=state,
        health=health,
        credential=CredentialHandle(object() if secret is None else secret)
        if state is CredentialResultState.SUCCESS
        else None,
        code=code.value if code is not None else None,
        message="safe credential result",
        refreshed=state is CredentialResultState.SUCCESS,
    )


def _install_lock(monkeypatch: pytest.MonkeyPatch, held: dict[str, bool]) -> _FakeLock:
    lock = _FakeLock(held)

    def acquire(account_id: str, operation: str) -> _FakeLock:
        assert account_id == ACCOUNT_ID
        assert operation == "token-refresh"
        return lock

    monkeypatch.setattr(auth_cli, "_acquire_auth_lock", acquire)
    return lock


def test_refresh_uses_supervisor_and_explicit_validation_recovery_under_full_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"value": False}
    lock = _install_lock(monkeypatch, held)
    validation_health = _health(CredentialHealthState.NEEDS_VALIDATION)
    healthy = _health(CredentialHealthState.HEALTHY)
    supervisor = _FakeSupervisor(
        held,
        health=validation_health,
        result=_result(CredentialResultState.SUCCESS, healthy),
    )

    async def get_account(account_id: str):
        assert held["value"] is True
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(desired_state="stopped")

    monkeypatch.setattr(auth_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", lambda: supervisor)

    auth_cli.refresh(account_id=ACCOUNT_ID)

    assert supervisor.inspect_calls == [ACCOUNT_ID]
    assert supervisor.refresh_calls == [(ACCOUNT_ID, True)]
    assert lock.release_calls == 1
    assert held["value"] is False


def test_refresh_running_worker_fails_closed_before_supervisor_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"value": False}
    lock = _install_lock(monkeypatch, held)
    built = False

    async def get_account(account_id: str):
        assert held["value"] is True
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(desired_state="running")

    def forbidden_supervisor():
        nonlocal built
        built = True
        raise AssertionError("running worker must fail before credential mutation")

    monkeypatch.setattr(auth_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", forbidden_supervisor)

    with pytest.raises(typer.Exit) as exc_info:
        auth_cli.refresh(account_id=ACCOUNT_ID)

    assert exc_info.value.exit_code == 2
    assert built is False
    assert lock.release_calls == 1
    assert held["value"] is False


def test_refresh_failure_never_directly_clears_risk_and_never_prints_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"value": False}
    _install_lock(monkeypatch, held)
    health = _health(CredentialHealthState.HEALTHY)
    supervisor = _FakeSupervisor(
        held,
        health=health,
        result=_result(
            CredentialResultState.TERMINAL_FAILURE,
            health,
            code=CredentialFailureCode.AUTH_FAILED,
        ),
    )
    output: list[str] = []

    async def get_account(_account_id: str):
        assert held["value"] is True
        return SimpleNamespace(desired_state="stopped")

    async def forbidden_clear(_account_id: str) -> bool:
        raise AssertionError("CLI must not clear validation risk directly")

    monkeypatch.setattr(auth_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(auth_cli.worker_risk, "clear_after_refresh", forbidden_clear)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", lambda: supervisor)
    monkeypatch.setattr(auth_cli.console, "print", lambda value, **_kwargs: output.append(str(value)))

    with pytest.raises(typer.Exit) as exc_info:
        auth_cli.refresh(account_id=ACCOUNT_ID)

    assert exc_info.value.exit_code == 2
    assert supervisor.refresh_calls == [(ACCOUNT_ID, False)]
    rendered = "\n".join(output)
    assert "AUTH_FAILED" in rendered
    assert "TOP-SECRET" not in rendered


def test_live_worker_lock_conflict_prevents_cross_component_refresh_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = False

    def locked(_account_id: str, _operation: str):
        raise typer.Exit(code=2)

    def forbidden_supervisor():
        nonlocal built
        built = True
        raise AssertionError("lifecycle lock conflict must prevent mutation")

    monkeypatch.setattr(auth_cli, "_acquire_auth_lock", locked)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", forbidden_supervisor)

    with pytest.raises(typer.Exit) as exc_info:
        auth_cli.refresh(account_id=ACCOUNT_ID)

    assert exc_info.value.exit_code == 2
    assert built is False
