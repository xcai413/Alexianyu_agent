"""`xianyu-agent backup ...`:运营配置的导出与导入。

备份只含配置结构与文案:凭据(扫码 Cookie、IM Token、发货 API 密钥、AI Key)与真实校准批准记录
都不进文件。换环境后凭据要重新扫码或重新填写,校准要按 `docs/待真实验证清单.md` 重新登记。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console

from xianyu_agent.application.backup import BackupService

app = typer.Typer(help="运营配置备份:导出/导入 JSON(不含凭据与校准记录)。")
console = Console()


@app.command("export")
def export_backup(
    out: Path = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        ..., "--out", "-o", help="输出 JSON 文件路径。"
    ),
    account_id: str = typer.Option("", "--account", "-a", help="只导出这个账号相关配置。"),
    include_cards: bool = typer.Option(
        False, "--include-cards", help="同时导出可用卡密正文(敏感,默认不含)。"
    ),
) -> None:
    """导出运营配置。默认不含任何凭据;卡密正文需要显式加 --include-cards。"""

    async def _build() -> dict[str, object]:
        return await BackupService().export_document(
            include_cards=include_cards, account_id=account_id or None
        )

    document = asyncio.run(_build())
    # 文件读写留在异步函数之外:CLI 是一次性动作,不需要为它引入 anyio。
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {key: len(value) for key, value in document.items() if isinstance(value, list)}
    console.print(f"[green]OK[/green] 已导出到 {target}")
    console.print(json.dumps(counts, ensure_ascii=False))
    if include_cards:
        console.print("[yellow]注意[/yellow] 备份包含卡密正文,请按敏感文件保管。")
    console.print(
        "[dim]凭据与真实校准记录不在备份里:换环境后要重新扫码/填密钥,并重新登记校准。[/dim]"
    )


@app.command("import")
def import_backup(
    source: Path = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        ..., "--in", "-i", help="备份 JSON 文件路径。"
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="默认只预演,加 --apply 才写入。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认写入本地配置。"),
) -> None:
    """导入运营配置;同名对象跳过,不覆盖、不删除。"""
    if not dry_run and not yes:
        console.print("[red]真实导入会写入本地配置,请加 --yes(先跑一次默认 Dry Run 更稳)。[/red]")
        raise typer.Exit(code=1)

    path = Path(source)
    if not path.is_file():
        console.print(f"[red]备份文件不存在: {path}[/red]")
        raise typer.Exit(code=1)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]备份文件不是合法 JSON: {exc.msg}[/red]")
        raise typer.Exit(code=1) from exc

    async def _run() -> None:
        try:
            report = await BackupService().import_document(document, dry_run=dry_run)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        mode = "预演" if dry_run else "导入"
        console.print(
            f"[green]OK[/green] {mode}完成:新建 {report.total_created} 项,"
            f"跳过已存在 {report.total_skipped} 项。"
        )
        if report.created:
            console.print(f"新建明细:{json.dumps(report.created, ensure_ascii=False)}")
        if dry_run:
            console.print("[dim]这是 Dry Run:没有写入任何配置。加 --apply --yes 才会生效。[/dim]")
        else:
            console.print(
                "[dim]擦亮/求花等真实动作仍然停用:校准批准记录不在备份里,需要重新登记。[/dim]"
            )

    asyncio.run(_run())
