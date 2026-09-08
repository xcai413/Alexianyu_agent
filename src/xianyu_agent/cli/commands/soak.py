"""`xianyu-agent soak` P0-E 长稳验收控制。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.health.soak import (
    SoakRun,
    evidence_path,
    latest_soak,
    start_soak,
    stop_soak,
)
from xianyu_agent.utils.time_utils import format_duration, format_local

app = typer.Typer(help="P0-E 可恢复长稳验收(由 Windows Watchdog 每分钟采样)。")
console = Console()


@app.command("start")
def start(
    account_id: str = typer.Option(..., "--account", "-a"),
    hours: float = typer.Option(24.0, "--hours", min=0.01),
) -> None:
    """启动长稳验收;要求 daemon 与账号当前健康在线。"""
    try:
        run = asyncio.run(start_soak(account_id=account_id, hours=hours))
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]启动失败:{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]OK[/green] 已启动长稳验收 run={run.run_id[:12]} account={account_id}")
    console.print(f"目标时长:{hours:.2f}h;证据:{evidence_path(run.run_id)}")


@app.command("status")
def status(
    output: str = typer.Option("table", "--output", "-o", help="table 或 json。"),
) -> None:
    """查看最近一次长稳验收的持久化进度。"""
    if output not in {"table", "json"}:
        console.print("[red]--output 只支持 table 或 json。[/red]")
        raise typer.Exit(code=2)
    run = asyncio.run(latest_soak())
    if run is None:
        console.print("[dim]尚无长稳验收记录。[/dim]")
        return
    if output == "json":
        console.print_json(json.dumps(_as_dict(run), ensure_ascii=False))
    else:
        _print_table(run)


@app.command("stop")
def stop(
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """提前停止当前长稳验收;该次不会记为通过。"""
    if not yes and not typer.confirm("确认提前停止 P0-E 长稳验收?"):
        raise typer.Abort()
    run = asyncio.run(stop_soak())
    if run is None:
        console.print("[yellow]没有运行中的长稳验收。[/yellow]")
        return
    console.print(f"[green]OK[/green] 已停止 run={run.run_id[:12]},状态={run.status}")


def _as_dict(run: SoakRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "account_id": run.account_id,
        "started_at": format_local(run.started_at),
        "finished_at": format_local(run.finished_at),
        "elapsed_s": _elapsed(run),
        "evidence_path": str(evidence_path(run.run_id)),
        **run.params,
    }


def _print_table(run: SoakRun) -> None:
    params = run.params
    table = Table(title="P0-E 长稳验收")
    table.add_column("run")
    table.add_column("status")
    table.add_column("account")
    table.add_column("elapsed/target")
    table.add_column("samples")
    table.add_column("healthy/errors")
    table.add_column("reconnects")
    table.add_column("instance_changes")
    table.add_column("duplicates")
    table.add_column("secret_hits")
    table.add_row(
        run.run_id[:12],
        run.status,
        run.account_id,
        f"{format_duration(_elapsed(run))}/{format_duration(float(params['target_duration_s']))}",
        str(params.get("sample_count", 0)),
        f"{params.get('healthy_samples', 0)}/{params.get('error_samples', 0)}",
        str(params.get("max_reconnects", 0)),
        str(params.get("daemon_instance_changes", 0)),
        str(params.get("duplicate_reply_groups", 0)),
        str(params.get("sensitive_log_hits", 0)),
    )
    console.print(table)
    console.print(
        f"消息:in={params.get('inbound_messages', 0)} out={params.get('outbound_messages', 0)};"
        f"回复:success={params.get('successful_replies', 0)} failed={params.get('failed_replies', 0)};"
        f"stale={params.get('stale_samples', 0)} outage={params.get('outage_count', 0)} "
        f"max_outage={format_duration(float(params.get('max_outage_s', 0.0)))}"
    )
    console.print(f"开始:{format_local(run.started_at)};证据:{evidence_path(run.run_id)}")
    if params.get("issues"):
        console.print(f"[red]问题:{'; '.join(str(item) for item in params['issues'])}[/red]")


def _elapsed(run: SoakRun) -> float:
    start = run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at
    end = run.finished_at or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0.0, (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds())
