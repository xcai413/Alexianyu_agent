"""`xianyu-agent doctor` 本地健康检查。"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.services.doctor import DoctorReport, run_doctor

console = Console()


def doctor(
    output: str = typer.Option("table", "--output", "-o", help="table 或 json。"),
) -> None:
    """检查数据库、迁移、密钥、Cookie、日志、daemon 与任务计划。"""
    if output not in {"table", "json"}:
        console.print("[red]--output 只支持 table 或 json。[/red]")
        raise typer.Exit(code=2)
    report = asyncio.run(run_doctor())
    if output == "json":
        console.print_json(json.dumps(_as_dict(report), ensure_ascii=False))
    else:
        _print_table(report)
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


def _print_table(report: DoctorReport) -> None:
    table = Table(title="xianyu-agent doctor")
    table.add_column("status")
    table.add_column("check")
    table.add_column("summary")
    table.add_column("detail", overflow="fold")
    styles = {"pass": "green", "warn": "yellow", "fail": "red"}
    for check in report.checks:
        table.add_row(
            f"[{styles[check.status]}]{check.status.upper()}[/{styles[check.status]}]",
            check.name,
            check.summary,
            check.detail or "-",
        )
    console.print(table)
    counts = report.counts
    console.print(
        f"结果:PASS={counts['pass']} WARN={counts['warn']} FAIL={counts['fail']}"
    )


def _as_dict(report: DoctorReport) -> dict[str, object]:
    return {
        "ok": report.exit_code == 0,
        "counts": report.counts,
        "checks": [
            {
                "name": check.name,
                "status": check.status,
                "summary": check.summary,
                "detail": check.detail,
            }
            for check in report.checks
        ],
    }
