"""`xianyu-agent auth ...` subcommands."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import qrcode as qrcode_lib
import qrcode.constants
import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.session import (
    CredentialFailureCode,
    CredentialHealthState,
    CredentialResultState,
    CredentialSupervisor,
    QrLoginAccountBusyError,
    QrLoginApplication,
    QrLoginResult,
    QrLoginResultState,
)
from xianyu_agent.config import get_settings
from xianyu_agent.domain import accounts as domain_accounts, worker_risk
from xianyu_agent.domain.account import lease as account_lease
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.infrastructure.session import LegacyValidationGate, LegacyWsCredentialBackend
from xianyu_agent.protocol.auth.qr import QRLoginClient, QRLoginSession
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.protocol.ws_auth import WsAuthError, WsTokenProvider
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.services.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="管理账号 Cookie 与 token 签名。")
console = Console()
_PROBE_CLEANUP_TIMEOUT_S = 10.0
_PROBE_LEASE_RENEW_INTERVAL_S = 20.0
_recovery_guards: set[asyncio.Task[None]] = set()
_pending_probe_cleanup: set[asyncio.Task[None]] = set()


@app.command("login")
def login(
    account_id: str = typer.Option(..., "--account", "-a", help="闲鱼账号 ID(自己取名)。"),
    cookie: str = typer.Option(
        ..., "--cookie", "-c", help="完整 Cookie 字符串(包含 unb / _m_h5_tk / cookie2 等)。"
    ),
    remark: str = typer.Option("", "--remark", "-r", help="备注,可空。"),
) -> None:
    """保存账号 + 加密 Cookie(若账号不存在则新建)。"""

    async def _run() -> None:
        account = await domain_accounts.get_account(account_id)
        if account is None:
            account = await domain_accounts.create_account(
                account_id,
                nickname=None,
                remark=remark or None,
                enabled=True,
            )
        elif remark:
            await domain_accounts.set_remark(account_id, remark)
        if account.desired_state == "running":
            console.print(
                "[red]账号 Worker 期望状态为 running。[/red] 请先执行 "
                f"[cyan]pool stop --account {account_id}[/cyan]。"
            )
            raise typer.Exit(code=2)
        signer = CookieSigner()
        ok = await signer.save_cookie(account_id, cookie)
        if not ok:
            console.print(f"[red]保存失败:账号 {account_id} 不存在或 Fernet Key 未配置。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 账号 [cyan]{account_id}[/cyan] Cookie 已加密保存。")

    lock = _acquire_auth_lock(account_id, "manual-login")
    try:
        asyncio.run(_run())
    finally:
        lock.release()


@app.command("status")
def status(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """显示账号 token 状态(脱敏)。"""

    async def _run() -> None:
        signer = CookieSigner()
        fp = await signer.fingerprint(account_id)
        if fp is None:
            # 两种情况要分开说:账号不存在要去 `account add`/`auth login`,账号存在但没 Cookie
            # 只需要补登录。混成一句话会让运营找错方向。
            account = await domain_accounts.get_account(account_id)
            if account is None:
                console.print(
                    f"[red]账号 {account_id} 不存在[/red];先 `account add` 或 `auth login`。"
                )
            else:
                console.print(
                    f"[yellow]账号 {account_id} 还没有 Cookie[/yellow];"
                    f"先用 `auth qr-login -a {account_id}` 或 `auth login -a {account_id}` 登录。"
                )
            raise typer.Exit(code=1)
        table = Table(title=f"账号 {account_id}", show_header=False)
        table.add_column("field", style="cyan")
        table.add_column("value")
        for k, v in fp.items():
            table.add_row(k, str(v))
        ws_status = await WsTokenProvider(signer).status(account_id)
        table.add_row("ws_credential_exists", "Y" if ws_status.exists else "N")
        table.add_row("ws_token_cached", "Y" if ws_status.token_cached else "N")
        table.add_row("ws_token_valid", "Y" if ws_status.valid else "N")
        table.add_row("ws_device_id", ws_status.device_id_masked or "-")
        table.add_row("ws_token_expires_at", format_local(ws_status.expires_at) or "-")
        risk = await worker_risk.get(account_id)
        if risk is not None:
            table.add_row("ws_risk_code", risk.code)
            table.add_row("ws_risk_cooldown_until", format_local(risk.cooldown_until) or "-")
            table.add_row("ws_risk_recovery_required", "Y")
        console.print(table)

    lock = _acquire_auth_lock(account_id, "token-refresh")
    try:
        asyncio.run(_run())
    finally:
        lock.release()


@app.command("refresh")
def refresh(
    account_id: str = typer.Option(..., "--account", "-a"),
    probe_timeout_s: float = typer.Option(
        45.0,
        "--probe-timeout",
        min=5.0,
        help="人工验证后等待 WebSocket 会话完成验证的秒数。",
    ),
) -> None:
    """刷新凭据,并在风控恢复时验证真实 WebSocket 会话。"""

    async def _run() -> None:
        account = await domain_accounts.get_account(account_id)
        if account is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        if account.desired_state == "running":
            console.print(
                "[red]账号 Worker 期望状态为 running。[/red] 请先执行 "
                f"[cyan]pool stop --account {account_id}[/cyan],避免并发刷新。"
            )
            raise typer.Exit(code=2)
        owner = await account_lease.live_owner(account_id)
        if owner is not None:
            console.print(
                f"[red]账号当前由实例 {owner[:8]} 持有(M2 账号租约)。[/red] "
                f"请在该实例上执行 [cyan]pool stop --account {account_id}[/cyan] 后再刷新。"
            )
            raise typer.Exit(code=2)

        credentials = _build_credential_supervisor()
        health = await credentials.inspect(account_id)
        validation_recovery = health.state is CredentialHealthState.NEEDS_VALIDATION
        if validation_recovery and not health.validation_cooling:
            timeout_s = probe_timeout_s if isinstance(probe_timeout_s, (int, float)) else 45.0
            recovered, reason = await _run_leased_validation_probe(
                account_id, credentials, timeout_s=float(timeout_s), account_lock=lock
            )
            if recovered:
                console.print("[green]验证熔断已解除;WebSocket 会话已验证并进入 ONLINE。[/green]")
                return
            console.print(
                f"[yellow]凭据刷新后仍未完成会话恢复;风控闸门保持开启。[/yellow] 原因:{reason}"
            )
            raise typer.Exit(code=2)

        result = await credentials.refresh(
            account_id,
            validation_recovery=validation_recovery,
        )

        if result.state is CredentialResultState.SUCCESS:
            if validation_recovery:
                console.print("[green]验证熔断已解除;可在确认后启动该账号 Worker。[/green]")
            console.print(
                f"[green]OK[/green] 账号 [cyan]{account_id}[/cyan] IM Token 已加密缓存;"
                f"有效期至 {format_local(result.health.expires_at)}。"
            )
            return

        if result.state is CredentialResultState.NEEDS_VALIDATION:
            if result.health.validation_cooling:
                console.print(
                    "[yellow]账号仍处于 FAIL_SYS_USER_VALIDATE 验证冷却。[/yellow] "
                    "完成 App 人工验证并等待冷却结束后,再执行一次 auth refresh。"
                )
            else:
                console.print(
                    "[yellow]账号仍需要 App 人工验证。[/yellow] 完成验证后再执行一次 auth refresh。"
                )
            raise typer.Exit(code=2)

        detail = result.code or result.message or "credential refresh failed"
        if result.code == CredentialFailureCode.CREDENTIAL_MISSING.value:
            console.print("[red]无 Cookie 可刷新。[/red] 请先 [cyan]auth login[/cyan]。")
            raise typer.Exit(code=1)
        console.print(f"[red]IM Token 刷新失败:{detail}[/red]")
        if result.code == CredentialFailureCode.SESSION_EXPIRED.value:
            console.print(f"请执行 [cyan]auth qr-login --account {account_id}[/cyan] 重新扫码。")
        raise typer.Exit(code=2)

    # The same process-wide account lock used by Worker/QR/manual auth covers the
    # entire desired-state check + credential mutation window.  This closes the
    # check-then-act race where a Worker could start between the guard and refresh.
    lock = _acquire_auth_lock(account_id, "token-refresh")
    try:
        asyncio.run(_run())
    finally:
        lock.release()


@app.command("list")
def list_accounts() -> None:
    """列出所有账号。"""

    async def _run() -> None:
        rows = await domain_accounts.list_accounts()
        if not rows:
            console.print("[dim]暂无账号,先 [cyan]auth login --account <id>[/cyan]。[/dim]")
            return
        table = Table(title=f"账号列表 ({len(rows)})")
        table.add_column("account_id", style="cyan")
        table.add_column("nickname")
        table.add_column("remark")
        table.add_column("enabled")
        table.add_column("status", style="magenta")
        table.add_column("last_login_at")
        for r in rows:
            table.add_row(
                r.account_id,
                r.nickname or "-",
                r.remark or "-",
                "Y" if r.enabled else "N",
                r.status,
                format_local(r.last_login_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """查看账号详情(包含字段)。"""

    async def _run() -> None:
        r = await domain_accounts.get_account(account_id)
        if r is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"账号 {account_id}")
        table.add_column("字段", style="cyan")
        table.add_column("值")
        for col in (
            "account_id",
            "nickname",
            "remark",
            "enabled",
            "status",
            "last_login_at",
            "last_heartbeat_at",
            "created_at",
            "updated_at",
        ):
            val = getattr(r, col)
            if isinstance(val, bool):
                val = "Y" if val else "N"
            table.add_row(col, str(val) if val is not None else "-")
        console.print(table)

    asyncio.run(_run())


@app.command("enable")
def enable(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """启用账号。"""

    async def _run() -> None:
        if not await domain_accounts.set_enabled(account_id, True):
            console.print("[red]账号不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] {account_id} 已启用。")

    asyncio.run(_run())


@app.command("disable")
def disable(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """禁用账号(Worker 不会启动)。"""

    async def _run() -> None:
        if not await domain_accounts.set_enabled(account_id, False):
            console.print("[red]账号不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]OK[/yellow] {account_id} 已禁用。")

    asyncio.run(_run())


@app.command("delete")
def delete(
    account_id: str = typer.Option(..., "--account", "-a"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """删除账号及其 Cookie / 消息 / 订单(级联)。"""
    if not yes:
        confirm = typer.confirm(f"确认删除账号 {account_id}?(级联删除其 Cookie / 消息 / 订单)")
        if not confirm:
            raise typer.Abort()

    async def _run() -> None:
        if not await domain_accounts.delete_account(account_id):
            console.print("[red]账号不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[red]OK[/red] {account_id} 已删除。")

    asyncio.run(_run())


@app.command("qr-login")
def qr_login(
    account_id: str = typer.Option(..., "--account", "-a", help="登录后保存到的账号标识。"),
    timeout: float = typer.Option(300.0, "--timeout", help="等待扫码总时长(秒)。"),
    qr_out: str = typer.Option(
        "", "--qr-out", help="二维码 PNG 保存路径;默认 data/qr_logins/<session>.png。"
    ),
    remark: str = typer.Option("", "--remark", "-r", help="账号备注(可选)。"),
) -> None:
    """扫码登录闲鱼账号:生成二维码 -> 手机扫码确认 -> 自动保存 Cookie。"""

    async def _run() -> None:
        use_case = _build_qr_login_application()
        console.print("[dim]正在生成二维码...[/dim]")
        try:
            challenge = await use_case.start(account_id)
        except QrLoginAccountBusyError as exc:
            console.print(
                "[red]账号 Worker 期望状态为 running。[/red] 请先执行 "
                f"[cyan]pool stop --account {account_id}[/cyan]。"
            )
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            console.print(f"[red]生成二维码失败:{detail}[/red]")
            raise typer.Exit(code=1) from exc

        out_path = (
            Path(qr_out)
            if qr_out
            else (get_settings().data_dir / "qr_logins" / f"{challenge.session_id}.png")
        )
        try:
            qr = qrcode_lib.QRCode(
                version=5,
                error_correction=qrcode_lib.constants.ERROR_CORRECT_L,
                box_size=8,
                border=2,
            )
            qr.add_data(challenge.qr_content)
            qr.make()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            qr.make_image().save(out_path)
            console.print(f"\n[green]二维码已保存:[/green] {out_path}")
        except Exception as exc:
            console.print(f"[red]二维码 PNG 保存失败:{type(exc).__name__}: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        try:
            _print_qr_ascii(qr)
        except Exception as exc:
            console.print(f"[yellow]终端二维码预览失败(不影响 PNG):{type(exc).__name__}[/yellow]")

        console.print(
            f"请用闲鱼 App 扫码(会话 {challenge.session_id[:8]}...),等待 {timeout:.0f} 秒..."
        )
        last_status = "waiting"
        status_map = {
            "scanned": "已扫码,请在手机上确认",
            "success": "登录成功",
            "expired": "二维码已过期",
            "cancelled": "已取消",
            "verification_required": "需要手机验证",
        }

        def on_status(status: str) -> None:
            nonlocal last_status
            if status != last_status and status in status_map:
                console.print(f"[cyan]{status_map[status]}[/cyan]")
                last_status = status

        result = await use_case.finish(
            challenge,
            remark=remark,
            timeout_s=timeout,
            on_status=on_status,
        )
        _render_qr_login_result(result)

    lock = _acquire_auth_lock(account_id, "qr-login")
    try:
        asyncio.run(_run())
    finally:
        lock.release()


def _build_credential_supervisor(
    *,
    signer: CookieSigner | None = None,
    token_provider: WsTokenProvider | None = None,
) -> CredentialSupervisor:
    """Build the canonical credential mutation boundary for CLI auth flows."""
    signer = signer or CookieSigner()
    return CredentialSupervisor(
        LegacyWsCredentialBackend(signer=signer, provider=token_provider),
        LegacyValidationGate(),
    )


class RecoveryProbeCleanupError(RuntimeError):
    """Transport closure could not be proven; account ownership must be retained."""


class RecoveryLeaseLostError(RuntimeError):
    """The recovery process cannot prove continued exclusive ownership."""


def _observe_guard(task: asyncio.Task[None]) -> None:
    _recovery_guards.discard(task)
    if not task.cancelled():
        task.exception()


async def _renew_recovery_lease(account_id: str, owner: str, ttl: timedelta) -> None:
    while True:
        try:
            renewed = await account_lease.renew(account_id, instance_id=owner, ttl=ttl)
        except Exception:
            raise RecoveryLeaseLostError("recovery lease renewal failed") from None
        if not renewed:
            raise RecoveryLeaseLostError("recovery lease ownership lost")
        await asyncio.sleep(_PROBE_LEASE_RENEW_INTERVAL_S)


async def _run_leased_validation_probe(
    account_id: str,
    credentials: CredentialSupervisor,
    *,
    timeout_s: float,
    account_lock: AccountConnectionLock,
) -> tuple[bool, str]:
    recovery_owner = f"auth-recovery:{uuid.uuid4().hex}"
    ttl = timedelta(seconds=timeout_s + 120)
    claim = await account_lease.claim_for_start(
        account_id,
        instance_id=recovery_owner,
        ttl=ttl,
        require_approval=get_settings().takeover_require_approval,
    )
    if not claim.acquired:
        return False, f"恢复租约获取失败:{claim.reason}"
    release_lease = True
    guard = asyncio.create_task(_renew_recovery_lease(account_id, recovery_owner, ttl))
    _recovery_guards.add(guard)
    guard.add_done_callback(_observe_guard)
    probe = asyncio.create_task(
        _run_validation_recovery_probe(
            account_id, credentials, timeout_s=timeout_s, account_lock=account_lock
        )
    )
    try:
        done, _ = await asyncio.wait({guard, probe}, return_when=asyncio.FIRST_COMPLETED)
        if guard in done:
            await guard
        return await probe
    except RecoveryProbeCleanupError:
        release_lease = False
        return False, "恢复连接清理失败,账号占用已保留;停止该恢复进程后再检查租约"
    except RecoveryLeaseLostError:
        with suppress(Exception):
            await worker_risk.open_user_validate(account_id, code="RECOVERY_LEASE_LOST")
        return False, "恢复租约续租失败或占用丢失,已停止恢复;需人工检查"
    finally:
        if not probe.done():
            probe.cancel()
            try:
                await probe
            except RecoveryProbeCleanupError:
                release_lease = False
            except asyncio.CancelledError:
                pass
        if release_lease:
            guard.cancel()
            with suppress(asyncio.CancelledError, RecoveryLeaseLostError):
                await guard
            await account_lease.release(account_id, instance_id=recovery_owner)


async def _run_validation_recovery_probe(
    account_id: str,
    credentials: CredentialSupervisor,
    *,
    timeout_s: float,
    account_lock: AccountConnectionLock | None = None,
) -> tuple[bool, str]:
    """Run one bounded readiness probe; unproven cleanup retains ownership."""
    worker = AccountWorker(
        account_id,
        account_lock=account_lock.borrow() if account_lock else None,
        credential_supervisor=credentials,
        persist_events=False,
        automation_mode="passive",
    )

    async def failed(reason: str) -> tuple[bool, str]:
        with suppress(Exception):
            await worker_risk.record_recovery_probe(
                account_id,
                result="failed",
                reason=reason,
            )
        return False, reason

    deadline = asyncio.get_running_loop().time() + timeout_s
    failure_reason: str | None = None
    try:
        startup = worker.start()
        if startup is not None:
            await asyncio.wait_for(
                startup, timeout=max(0.001, deadline - asyncio.get_running_loop().time())
            )
        if worker.state is not WorkerState.NEEDS_VALIDATION:
            return await failed(f"持久化状态为 {worker.state.value},不是 NEEDS_VALIDATION")
        recovered = await asyncio.wait_for(
            worker.recover_validation(),
            timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
        )
        if not recovered:
            return await failed("验证恢复未能启动,请确认 App 验证已完成且冷却已结束")

        while asyncio.get_running_loop().time() < deadline:
            if worker.state is WorkerState.ONLINE:
                break
            if worker.state in {WorkerState.NEEDS_VALIDATION, WorkerState.ERROR}:
                return await failed(f"会话最终状态为 {worker.state.value}")
            await asyncio.sleep(0.1)
        else:
            return await failed(f"{timeout_s:.0f} 秒内未完成订阅就绪")
    except TimeoutError:
        failure_reason = f"{timeout_s:.0f} 秒内未完成凭据刷新或会话验证"
    except asyncio.CancelledError:
        await failed("恢复探测被取消,未确认会话恢复")
        raise
    except Exception as exc:
        # Backend exception messages may contain credentials or response bodies.
        failure_reason = f"恢复探测异常:{type(exc).__name__},未确认会话恢复"
    finally:
        await _stop_recovery_probe(account_id, worker, account_lock)
    if failure_reason is None:
        # Success is audited only after physical cleanup returned without error.
        with suppress(Exception):
            await worker_risk.record_recovery_probe(account_id, result="ok")
        return True, "online"
    return await failed(failure_reason)


async def _stop_recovery_probe(
    account_id: str, worker: AccountWorker, account_lock: AccountConnectionLock | None
) -> None:
    if worker.state is WorkerState.DISABLED:
        return
    cleanup = asyncio.create_task(worker.stop())
    _pending_probe_cleanup.add(cleanup)

    def observe_cleanup(task: asyncio.Task[None]) -> None:
        _pending_probe_cleanup.discard(task)
        if not task.cancelled():
            task.exception()

    cleanup.add_done_callback(observe_cleanup)
    try:
        await _wait_probe_cleanup(cleanup)
    except BaseException:
        cleanup.cancel()
        if account_lock is not None:
            account_lock.quarantine()
        with suppress(Exception):
            await worker_risk.open_user_validate(account_id, code="RECOVERY_CLEANUP_FAILED")
            await worker_risk.record_recovery_probe(
                account_id, result="failed", reason="恢复连接清理失败,未确认关闭"
            )
        raise RecoveryProbeCleanupError("recovery transport cleanup failed") from None


async def _wait_probe_cleanup(cleanup: asyncio.Task[None]) -> None:
    done, _ = await asyncio.wait({cleanup}, timeout=_PROBE_CLEANUP_TIMEOUT_S)
    if not done:
        raise TimeoutError("recovery cleanup deadline exceeded")
    await cleanup


def _build_qr_login_application(
    *,
    token_provider: WsTokenProvider | None = None,
) -> QrLoginApplication:
    """Wire the QR use case at the CLI composition boundary."""
    signer = CookieSigner()
    credentials = _build_credential_supervisor(
        signer=signer,
        token_provider=token_provider,
    )
    return QrLoginApplication(
        platform=QRLoginClient(),
        accounts=domain_accounts,
        cookies=signer,
        credentials=credentials,
    )


async def _persist_qr_login_session(
    account_id: str,
    remark: str,
    session: QRLoginSession,
    *,
    token_provider: WsTokenProvider | None = None,
) -> None:
    """Compatibility adapter for callers with an already-confirmed QR session."""
    result = await _build_qr_login_application(token_provider=token_provider).complete_confirmed(
        account_id,
        session,
        remark=remark,
    )
    _render_qr_login_result(result)


def _render_qr_login_result(result: QrLoginResult) -> None:
    """Render one secret-free application result and preserve CLI exit semantics."""
    if result.cookie_saved:
        console.print(
            f"[green]OK[/green] 扫码登录成功,账号 [cyan]{result.account_id}[/cyan] "
            "(闲鱼身份已确认),Cookie 已加密保存。"
        )

    if result.state is QrLoginResultState.SUCCESS:
        console.print(
            "[green]OK[/green] IM Token 已加密缓存,设备 ID 保持不变;"
            f"有效期至 {format_local(result.credential_expires_at)}。"
        )
        return
    if result.state is QrLoginResultState.VERIFICATION_REQUIRED:
        console.print(f"[red]账号被风控,需要手机验证:[/red] {result.verification_url or '未知'}")
        raise typer.Exit(code=2)
    if result.state is QrLoginResultState.NEEDS_VALIDATION:
        if result.validation_cooling:
            console.print(
                "[yellow]账号仍处于 FAIL_SYS_USER_VALIDATE 验证冷却,未自动请求 IM Token。[/yellow] "
                "完成 App 人工验证并等待冷却结束后,再执行一次 auth refresh。"
            )
        else:
            console.print(
                "[yellow]Cookie 已保存,风控恢复尚未通过会话验证。[/yellow] "
                "如 App 仍有验证提示,先完成验证;随后执行一次 auth refresh 验证连接。"
            )
        raise typer.Exit(code=3)
    if result.state in {
        QrLoginResultState.CREDENTIAL_RETRYABLE_FAILURE,
        QrLoginResultState.CREDENTIAL_TERMINAL_FAILURE,
    }:
        detail = result.credential_code or result.message or "credential refresh failed"
        console.print(f"[yellow]Cookie 已保存,但 IM Token 换取失败:{detail}[/yellow]")
        console.print(f"稍后执行 [cyan]auth refresh --account {result.account_id}[/cyan]。")
        raise typer.Exit(code=3)
    if result.state is QrLoginResultState.PERSISTENCE_FAILURE:
        console.print("[red]Cookie 保存失败。[/red]")
        raise typer.Exit(code=1)

    console.print(f"[red]扫码未完成: {result.status}[/red]")
    raise typer.Exit(code=1)


async def _record_user_validate_circuit(account_id: str, exc: WsAuthError) -> bool:
    """记录明确的 IM 人工验证错误;不对其他认证错误改变既有策略。"""
    if not worker_risk.is_user_validate_error(exc):
        return False
    circuit = await worker_risk.open_user_validate(account_id)
    if circuit is not None:
        console.print(
            "[yellow]已暂停该账号的 WS 重试。[/yellow] "
            f"验证冷却至 {format_local(circuit.cooldown_until)};"
            "完成 App 人工验证后仅执行一次 auth refresh。"
        )
    return True


def _acquire_auth_lock(account_id: str, operation: str) -> AccountConnectionLock:
    lock = AccountConnectionLock(get_settings().account_lock_path(account_id))
    try:
        lock.acquire(owner_id=f"auth:{operation}:{uuid.uuid4().hex}")
    except AccountConnectionAlreadyRunningError as exc:
        console.print(
            f"[red]账号 {account_id} 正被另一个 WS/认证进程使用。[/red] "
            "请先停止对应 Worker 后重试。"
        )
        raise typer.Exit(code=2) from exc
    return lock


def _print_qr_ascii(qr: qrcode_lib.QRCode) -> None:
    """使用仅 ASCII 字符输出二维码,兼容 Windows GBK 终端。"""
    for row in qr.get_matrix():
        console.print("".join("##" if cell else "  " for cell in row), markup=False)
