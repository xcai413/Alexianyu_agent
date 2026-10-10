"""xianyu-agent delivery 子命令:发货策略与模板。

策略决定"这个订单从哪个库存池发、延迟多久、用什么模板"。解析是 Dry Run:先看清楚会命中哪条
策略,再让自动发货执行。模板渲染同样可以先试,白名单以外的变量会直接报错。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.delivery import (
    ApiConfigService,
    DeliveryExecutor,
    DeliveryGuardService,
    DeliveryJobService,
    DeliveryPolicyService,
    DeliveryRecoveryService,
    DeliveryRequest,
    DeliveryTemplateService,
)
from xianyu_agent.application.ports.secret_vault import SecretMetadata, SecretRef, SecretValue
from xianyu_agent.domain.delivery import (
    LEVEL_ORDER,
    ApiConfigError,
    ApiDeliveryConfig,
    DeliveryAttempt,
    DeliveryJob,
    DeliveryPolicy,
    DeliveryTemplate,
    GuardContext,
    GuardRule,
    InvalidJobTransition,
    TemplateError,
    render_template,
)
from xianyu_agent.domain.inventory.unit import masked_preview
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import binding_key
from xianyu_agent.domain.runtime import command as worker_commands
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
from xianyu_agent.runtime.delivery_sender import (
    make_consign_port,
    send_once,
    send_once_image,
)

app = typer.Typer(help="发货策略与模板(规格 > 商品 > 账号 > 全局)。")
policy_app = typer.Typer(help="发货策略:创建、查看、解析与停用。")
job_app = typer.Typer(help="发货任务:状态、尝试记录与人工收尾。")
template_app = typer.Typer(help="发货模板库:可被多条策略复用的发货文案。")
app.add_typer(policy_app, name="policy")
app.add_typer(job_app, name="job")
app.add_typer(template_app, name="template")
console = Console()


def _fail(message: object) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _parse_specs(values: Sequence[str]) -> list[tuple[str, str]]:
    """把重复出现的 规格名=规格值 选项解析成规格对。"""
    pairs: list[tuple[str, str]] = []
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name.strip() or not value.strip():
            msg = f"--spec 需要 规格名=规格值 形式: {item}"
            raise ValueError(msg)
        pairs.append((name.strip(), value.strip()))
    if not pairs:
        msg = "至少需要一个 --spec"
        raise ValueError(msg)
    return pairs


def _spec_key(values: Sequence[str]) -> str | None:
    return None if not values else binding_key(normalize_specs(_parse_specs(values)))


def _render_policies(policies: Sequence[DeliveryPolicy]) -> None:
    if not policies:
        console.print("[dim]暂无发货策略。[/dim]")
        return
    table = Table(title=f"发货策略 ({len(policies)})")
    for column in ("id", "name", "level", "scope", "pool", "delay", "mode", "priority", "enabled"):
        table.add_column(column)
    for policy in policies:
        table.add_row(
            str(policy.local_id),
            policy.name,
            policy.level,
            policy.scope_key(),
            str(policy.inventory_pool_id),
            f"{policy.delay_seconds}s",
            policy.delivery_mode or "-",
            str(policy.priority),
            "Y" if policy.enabled else "N",
        )
    console.print(table)


@policy_app.command("create")
def create_policy(  # noqa: PLR0917 (CLI option surface)
    name: str = typer.Option(..., "--name", "-n"),
    pool_id: int = typer.Option(..., "--pool", "-p", help="库存池 id。"),
    account_id: str = typer.Option("", "--account", "-a", help="限定账号(可选)。"),
    item_id: str = typer.Option("", "--item-id", help="限定商品(可选)。"),
    spec: list[str] = typer.Option([], "--spec", help="限定规格,形如 颜色=红色,可重复"),  # noqa: B008
    delay_seconds: int = typer.Option(0, "--delay", min=0, max=3600, help="发货前延迟秒数。"),
    mode: str = typer.Option("", "--mode", help="发货模式(text/data/image/api),留空以池类型为准。"),
    template: str = typer.Option("", "--template", help="发货模板,只用白名单变量。"),
    template_ref: str = typer.Option(
        "", "--template-ref", help="引用模板库里的模板(名称或 id);与 --template 同时给出时以内联为准。"
    ),
    platform_confirm: bool = typer.Option(
        False, "--platform-confirm", help="发货后需要平台确认动作。"
    ),
    priority: int = typer.Option(0, "--priority", min=0, help="同级平局时数值大者优先。"),
    description: str = typer.Option("", "--desc"),
    enabled: bool = typer.Option(True, "--enabled/--disabled"),
) -> None:
    """新建发货策略;范围越具体层级越高。"""

    async def _run() -> None:
        try:
            policy = await DeliveryPolicyService().create(
                name=name,
                inventory_pool_id=pool_id,
                account_id=account_id or None,
                item_id=item_id or None,
                sku_id=_spec_key(spec),
                delay_seconds=delay_seconds,
                delivery_mode=mode or None,
                template=template or None,
                template_ref=template_ref or None,
                platform_confirm=platform_confirm,
                priority=priority,
                enabled=enabled,
                description=description or None,
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] {policy.describe()} 层级 {policy.level}")

    asyncio.run(_run())


@policy_app.command("list")
def list_policies(
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的。"),
    account_id: str = typer.Option("", "--account", "-a"),
    item_id: str = typer.Option("", "--item-id"),
) -> None:
    """列出发货策略。"""

    async def _run() -> None:
        policies = await DeliveryPolicyService().list(
            only_enabled=only_enabled,
            account_id=account_id or None,
            item_id=item_id or None,
        )
        _render_policies(policies)

    asyncio.run(_run())


@policy_app.command("show")
def show_policy(policy_id: int = typer.Argument(...)) -> None:
    """查看一条策略(含模板与所需变量)。"""

    async def _run() -> None:
        try:
            policy = await DeliveryPolicyService().get(policy_id)
        except ValueError as exc:
            _fail(exc)
            return
        _render_policies([policy])
        console.print(f"层级 {policy.level} | 范围 {policy.scope_key()}")
        console.print(
            f"平台确认 {'Y' if policy.platform_confirm else 'N'} | "
            f"优先级 {policy.priority} | 启用 {policy.enabled}"
        )
        if policy.template is not None:
            console.print(f"模板:{policy.template}")
            console.print(f"使用变量:{'、'.join(policy.template_variables) or '(无)'}")
        if policy.description:
            console.print(f"说明:{policy.description}")

    asyncio.run(_run())


@policy_app.command("set")
def set_policy(  # noqa: PLR0917 (CLI option surface)
    policy_id: int = typer.Argument(...),
    name: str = typer.Option("", "--name", help="新名称;留空不改。"),
    pool_id: int | None = typer.Option(None, "--pool", help="新的库存池 id。"),
    account_id: str = typer.Option("", "--account", "-a", help="新的账号范围;留空不改。"),
    item_id: str = typer.Option("", "--item-id", help="新的商品范围;留空不改。"),
    spec: list[str] = typer.Option([], "--spec", help="新的规格范围;可重复。"),  # noqa: B008
    delay_seconds: int | None = typer.Option(None, "--delay", min=0, max=3600),
    mode: str = typer.Option("", "--mode", help="新的发货模式;留空不改。"),
    template: str = typer.Option("", "--template", help="新的内联模板;留空不改。"),
    template_ref: str = typer.Option("", "--template-ref", help="新的模板引用;留空不改。"),
    platform_confirm: bool | None = typer.Option(
        None, "--platform-confirm/--no-platform-confirm", help="是否启用平台确认。"
    ),
    priority: int | None = typer.Option(None, "--priority", min=0),
    description: str = typer.Option("", "--desc", help="新的说明;留空不改。"),
    clear_scope: bool = typer.Option(False, "--clear-scope", help="清除账号/商品/规格范围。"),
) -> None:
    """修改发货策略;不直接触发发货。"""
    if not any(
        (
            name,
            pool_id is not None,
            account_id,
            item_id,
            spec,
            delay_seconds is not None,
            mode,
            template,
            template_ref,
            platform_confirm is not None,
            priority is not None,
            description,
            clear_scope,
        )
    ):
        _fail("至少提供一项修改内容")

    async def _run() -> None:
        try:
            policy = await DeliveryPolicyService().update(
                policy_id,
                name=name or None,
                inventory_pool_id=pool_id,
                account_id=account_id or None,
                item_id=item_id or None,
                sku_id=_spec_key(spec) if spec else None,
                delay_seconds=delay_seconds,
                delivery_mode=mode or None,
                template=template or None,
                template_ref=template_ref or None,
                platform_confirm=platform_confirm,
                priority=priority,
                description=description or None,
                clear_scope=clear_scope,
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] {policy.describe()} 已更新。")

    asyncio.run(_run())


@policy_app.command("enable")
def enable_policy(policy_id: int = typer.Argument(...)) -> None:
    """启用策略。"""

    async def _run() -> None:
        try:
            policy = await DeliveryPolicyService().set_enabled(policy_id, enabled=True)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] {policy.describe()} 已启用。")

    asyncio.run(_run())


@policy_app.command("disable")
def disable_policy(policy_id: int = typer.Argument(...)) -> None:
    """停用策略。"""

    async def _run() -> None:
        try:
            policy = await DeliveryPolicyService().set_enabled(policy_id, enabled=False)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[yellow]OK[/yellow] {policy.describe()} 已停用。")

    asyncio.run(_run())


@policy_app.command("delete")
def delete_policy(
    policy_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除策略。"""
    if not yes and not typer.confirm(f"确认删除发货策略 #{policy_id}?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            await DeliveryPolicyService().delete(policy_id)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[red]OK[/red] 发货策略 #{policy_id} 已删除。")

    asyncio.run(_run())


@policy_app.command("resolve")
def resolve_policy_command(
    account_id: str = typer.Option("", "--account", "-a", help="账号(可选)。"),
    item_id: str = typer.Option("", "--item-id", help="商品(可选)。"),
    spec: list[str] = typer.Option([], "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
) -> None:
    """Dry Run:这次发货会命中哪条策略。"""

    async def _run() -> None:
        try:
            resolution = await DeliveryPolicyService().resolve(
                account_id=account_id or None,
                item_id=item_id or None,
                sku_id=_spec_key(spec),
            )
        except ValueError as exc:
            _fail(exc)
            return
        if resolution.policy is None:
            console.print(f"[yellow]未命中[/yellow] {resolution.reason}")
            return
        console.print(f"[green]命中 {resolution.level} 级[/green] {resolution.policy.describe()}")
        console.print(f"依据:{resolution.reason}")

    asyncio.run(_run())


@app.command("render")
def render_command(
    template: str = typer.Option(..., "--template", "-t", help="模板文本。"),
    var: list[str] = typer.Option([], "--var", help="变量,形如 buyer_name=张三,可重复"),  # noqa: B008
) -> None:
    """按白名单渲染一次模板(不会发送任何东西)。"""

    variables: dict[str, str] = {}
    for item in var:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            _fail(f"--var 需要 变量名=值 形式: {item}")
            return
        variables[name.strip()] = value
    try:
        console.print(render_template(template, variables))
    except TemplateError as exc:
        _fail(exc)
        return
    console.print(f"[dim]层级顺序:{' > '.join(LEVEL_ORDER)}[/dim]")


def _render_jobs(jobs: Sequence[DeliveryJob]) -> None:
    if not jobs:
        console.print("[dim]暂无发货任务。[/dim]")
        return
    table = Table(title=f"发货任务 ({len(jobs)})")
    for column in ("id", "account", "order", "state", "need", "reservation", "last_error"):
        table.add_column(column)
    for job in jobs:
        table.add_row(
            str(job.local_id),
            job.account_id,
            job.order_id,
            job.state,
            str(job.required_quantity),
            str(job.reservation_id or "-"),
            (job.last_error or "-")[:40],
        )
    console.print(table)


def _render_attempts(attempts: Sequence[DeliveryAttempt]) -> None:
    if not attempts:
        console.print("[dim]还没有外部动作记录。[/dim]")
        return
    table = Table(title=f"尝试记录 ({len(attempts)})")
    for column in ("no", "action", "result", "external_id", "error"):
        table.add_column(column)
    for attempt in attempts:
        table.add_row(
            str(attempt.attempt_no),
            attempt.action,
            attempt.result,
            attempt.external_message_id or "-",
            (attempt.error or "-")[:50],
        )
    console.print(table)


@job_app.command("list")
def list_jobs(
    account_id: str = typer.Option("", "--account", "-a"),
    order_id: str = typer.Option("", "--order-id", help="只看某个订单。"),
    state: str = typer.Option("", "--state", help="只看某个状态。"),
) -> None:
    """列出发货任务。"""

    async def _run() -> None:
        jobs = await DeliveryJobService().list(
            account_id=account_id or None, order_id=order_id or None, state=state or None
        )
        _render_jobs(jobs)

    asyncio.run(_run())


@job_app.command("show")
def show_job(job_id: int = typer.Argument(...)) -> None:
    """查看一个发货任务及其全部尝试记录。"""

    async def _run() -> None:
        service = DeliveryJobService()
        try:
            job = await service.get(job_id)
        except ValueError as exc:
            _fail(exc)
            return
        _render_jobs([job])
        if job.policy_snapshot:
            console.print(f"策略快照:{job.policy_snapshot}")
        _render_attempts(await service.attempts(job_id))

    asyncio.run(_run())


@job_app.command("retry")
def retry_job(
    job_id: int = typer.Argument(...),
    force: bool = typer.Option(
        False,
        "--force",
        help="仅重开最后一次明确未发送且库存已释放的 failed_final 任务。",
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """重试一个可重试的任务(会从订单再校验开始)。"""
    if not yes and not typer.confirm(f"确认重试发货任务 #{job_id}?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            job = await DeliveryJobService().retry(job_id, force=force)
        except (ValueError, InvalidJobTransition) as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 发货任务 #{job.local_id} 已回到 {job.state}。")

    asyncio.run(_run())


@job_app.command("cancel")
def cancel_job(
    job_id: int = typer.Argument(...),
    reason: str = typer.Option("", "--reason", help="取消原因(会写入任务)。"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """取消一个任务(库存退回由执行器负责)。"""
    if not yes and not typer.confirm(f"确认取消发货任务 #{job_id}?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            job = await DeliveryJobService().cancel(job_id, reason=reason or None)
        except (ValueError, InvalidJobTransition) as exc:
            _fail(exc)
            return
        console.print(f"[yellow]OK[/yellow] 发货任务 #{job.local_id} 已取消。")

    asyncio.run(_run())


@job_app.command("plan")
def plan_delivery(  # noqa: PLR0917 (CLI option surface)
    account_id: str = typer.Option(..., "--account", "-a"),
    order_id: str = typer.Option(..., "--order-id", help="订单 id。"),
    item_id: str = typer.Option("", "--item-id"),
    buyer_id: str = typer.Option("", "--buyer-id"),
    spec: list[str] = typer.Option([], "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
    quantity: int = typer.Option(0, "--quantity", min=0, help="覆盖发货数量(0 表示按订单)。"),
) -> None:
    """Dry Run:这次发货会不会放行、从哪个池取几份。"""

    async def _run() -> None:
        plan = await DeliveryExecutor().plan(
            DeliveryRequest(
                account_id=account_id,
                order_id=order_id,
                item_id=item_id or None,
                buyer_id=buyer_id or None,
                sku_ref=_spec_key(spec),
                selection=normalize_specs(_parse_specs(spec)) if spec else None,
                quantity=quantity or None,
            )
        )
        color = "green" if plan.allowed else "yellow"
        console.print(
            f"[{color}]{'可以发货' if plan.allowed else '不会发货'}[/{color}] {plan.reason}"
        )
        console.print(f"订单状态 {plan.order_status or '-'} | 守卫 {plan.guard_decision or '-'}")
        if plan.allowed:
            console.print(
                f"策略 {plan.policy_name}({plan.policy_level}) -> 池 #{plan.pool_id} "
                f"x{plan.quantity} | 延迟 {plan.delay_seconds}s | "
                f"平台确认 {'Y' if plan.platform_confirm else 'N'}"
            )
        if plan.hit_rules:
            console.print(f"命中规则:{'、'.join(plan.hit_rules)}")

    asyncio.run(_run())


@job_app.command("run")
def run_delivery(  # noqa: PLR0917 (CLI option surface)
    account_id: str = typer.Option(..., "--account", "-a"),
    order_id: str = typer.Option(..., "--order-id", help="订单 id。"),
    item_id: str = typer.Option("", "--item-id"),
    buyer_id: str = typer.Option("", "--buyer-id"),
    spec: list[str] = typer.Option([], "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
    quantity: int = typer.Option(0, "--quantity", min=0, help="覆盖发货数量(0 表示按订单)。"),
    send: bool = typer.Option(False, "--send", help="真的把卡密发给买家(默认只演练)。"),
    ignore_delay: bool = typer.Option(False, "--ignore-delay", help="忽略策略里的延迟。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行真实外发。"),
) -> None:
    """执行一次发货:默认 Dry Run;加 --send --yes 才会真的发出去。"""

    if send and not yes:
        _fail("真实发货会向买家发出消息,请加 --yes 明确确认")
        return

    async def _run() -> None:
        if send:
            try:
                command = await worker_commands.submit_delivery(
                    account_id,
                    order_id=order_id,
                    item_id=item_id or None,
                    quantity=quantity or None,
                    requested_by="cli",
                )
            except ValueError as exc:
                _fail(exc)
                return
            if command is None:
                _fail(f"账号 {account_id} 不存在")
                return
            console.print(
                f"[green]OK[/green] 已提交 daemon 执行发货:命令 {command.command_id} "
                f"(状态 {command.status})"
            )
            console.print("[dim]请用 `delivery job show/list` 查看执行结果。[/dim]")
            return

        request = DeliveryRequest(
            account_id=account_id,
            order_id=order_id,
            item_id=item_id or None,
            buyer_id=buyer_id or None,
            sku_ref=_spec_key(spec),
            selection=normalize_specs(_parse_specs(spec)) if spec else None,
            quantity=quantity or None,
            respect_delay=not ignore_delay,
        )
        executor = DeliveryExecutor(
            sender=send_once if send else None,
            image_sender=send_once_image if send else None,
            consign=make_consign_port() if send else None,
            waiter=None if ignore_delay else asyncio.sleep,
        )
        result = await executor.execute(request)
        console.print(
            f"[cyan]{result.decision}[/cyan] 任务 #{result.job.local_id} "
            f"状态 {result.job.state} | {result.reason}"
        )
        if result.reservation_id is not None:
            console.print(f"预留 #{result.reservation_id}({result.contents_sent} 条已发出)")
        if not send:
            console.print("[dim]这是演练:没有发送任何内容,也没有预留库存。[/dim]")

    asyncio.run(_run())


@job_app.command("recover")
def recover_jobs(
    account_id: str = typer.Option("", "--account", "-a"),
    apply: bool = typer.Option(False, "--apply", help="冻结结果未知的任务。"),
) -> None:
    """重启恢复:看非终态任务怎么处理;--apply 冻结结果未知的任务。"""

    async def _run() -> None:
        service = DeliveryRecoveryService()
        if apply:
            report = await service.freeze_in_flight(account_id=account_id or None)
            if report.frozen:
                console.print(
                    f"[yellow]已冻结 {len(report.frozen)} 个结果未知的任务:"
                    f"{'、'.join('#' + str(job_id) for job_id in report.frozen)}[/yellow]"
                )
        else:
            report = await service.scan(account_id=account_id or None)
        if not report.items:
            console.print("[dim]没有需要恢复的发货任务。[/dim]")
            return
        table = Table(title=f"待恢复任务 ({len(report.items)})")
        for column in ("id", "order", "state", "action", "reason"):
            table.add_column(column)
        for item in report.items:
            table.add_row(
                str(item.job.local_id),
                item.job.order_id,
                item.job.state,
                item.action,
                item.reason,
            )
        console.print(table)

    asyncio.run(_run())


@job_app.command("resolve")
def resolve_job(
    job_id: int = typer.Argument(...),
    delivered: bool = typer.Option(..., "--delivered/--not-delivered", help="外部到底收到没有。"),
    note: str = typer.Option(..., "--note", help="收尾依据,例如平台订单页已确认收到卡密。"),
    resolved_by: str = typer.Option("operator", "--by", help="收尾人标识。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认收尾。"),
) -> None:
    """人工收尾一条结果不明的发货任务(不触发任何平台动作)。

    收尾会同时落两件事:任务状态(已发出→completed,没发出→failed_retryable)与库存去向
    (核销/退回)。只做一半会留下永久冻结的卡密,所以这里一次做完;任务不是 `uncertain` 时
    直接拒绝,不覆盖自动流程写下的运行事实。
    """
    if not yes and not typer.confirm(
        f"确认按{'已发送' if delivered else '未发送'}收尾任务 #{job_id}?"
    ):
        raise typer.Abort()

    async def _run() -> None:
        try:
            job = await DeliveryJobService().resolve_uncertain(
                job_id, delivered=delivered, note=note, resolved_by=resolved_by
            )
        except (ValueError, InvalidJobTransition) as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 发货任务 #{job.local_id} 现在为 {job.state};"
            f"库存已按{'核销' if delivered else '退回'}处理。"
        )

    asyncio.run(_run())


@job_app.command("flag-uncertain")
def flag_uncertain_job(
    job_id: int = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="为什么无法确认买家是否收到。"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """把错误标记为完成的任务冻结为结果不明,不退回已消耗库存。"""
    if not yes and not typer.confirm(f"确认将发货任务 #{job_id} 标记为结果不明?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            job = await DeliveryJobService().flag_uncertain(job_id, reason=reason)
        except (ValueError, InvalidJobTransition) as exc:
            _fail(exc)
            return
        console.print(
            f"[yellow]OK[/yellow] 发货任务 #{job.local_id} 已冻结为 uncertain;"
            "原库存保持已消耗,不会复用。"
        )

    asyncio.run(_run())


guard_app = typer.Typer(help="发货守卫:黑名单、买家订单与未确认收货规则(fail closed)。")
blacklist_app = typer.Typer(help="买家黑名单:owner 全局、账号级或商品级。")
app.add_typer(guard_app, name="guard")
app.add_typer(blacklist_app, name="blacklist")


def _parse_config(values: Sequence[str]) -> dict[str, object]:
    """把 键=值 解析成规则配置:支持布尔、整数、逗号分隔列表与普通字符串。"""
    config: dict[str, object] = {}
    for item in values:
        key, separator, raw = item.partition("=")
        if not separator or not key.strip():
            msg = f"--config 需要 键=值 形式: {item}"
            raise ValueError(msg)
        value = raw.strip()
        if value.lower() in {"true", "false"}:
            config[key.strip()] = value.lower() == "true"
        elif value.isdigit():
            config[key.strip()] = int(value)
        elif "," in value:
            config[key.strip()] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            config[key.strip()] = value
    return config


def _render_guard_rules(rules: Sequence[GuardRule]) -> None:
    if not rules:
        console.print("[dim]暂无守卫规则。[/dim]")
        return
    table = Table(title=f"发货守卫规则 ({len(rules)})")
    for column in ("id", "scope", "rule", "priority", "config", "reason", "enabled"):
        table.add_column(column)
    for rule in rules:
        table.add_row(
            str(rule.local_id),
            rule.scope_key,
            rule.rule_code,
            str(rule.priority),
            str(dict(rule.config)) if rule.config else "-",
            (rule.block_reason or "-")[:30],
            "Y" if rule.enabled else "N",
        )
    console.print(table)


@guard_app.command("list")
def list_guard_rules(
    account_id: str = typer.Option("", "--account", "-a", help="只看该账号与全局规则。"),
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的规则。"),
) -> None:
    """列出守卫规则。"""

    async def _run() -> None:
        rules = await DeliveryGuardService().list_rules(
            account_id=account_id or None, only_enabled=only_enabled
        )
        _render_guard_rules(rules)

    asyncio.run(_run())


@guard_app.command("set")
def set_guard_rule(  # noqa: PLR0917 (CLI option surface)
    rule_code: str = typer.Option(..., "--rule", "-r", help="规则代号。"),
    account_id: str = typer.Option("", "--account", "-a", help="限定账号;留空为全局规则。"),
    priority: int = typer.Option(0, "--priority", min=0, help="命中时的优先级。"),
    reason: str = typer.Option("", "--reason", help="阻断原因(会展示给运营)。"),
    exclude_item: list[str] = typer.Option([], "--exclude-item", help="排除的商品 id,可重复"),  # noqa: B008
    config: list[str] = typer.Option([], "--config", help="规则参数,形如 threshold=2,可重复"),  # noqa: B008
    enabled: bool = typer.Option(True, "--enabled/--disabled"),
) -> None:
    """新增或更新一条守卫规则。"""

    async def _run() -> None:
        try:
            rule = await DeliveryGuardService().upsert_rule(
                rule_code=rule_code,
                account_id=account_id or None,
                enabled=enabled,
                priority=priority,
                block_reason=reason or None,
                excluded_item_ids=tuple(exclude_item),
                config=_parse_config(config),
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 规则 #{rule.local_id} {rule.rule_code} "
            f"范围 {rule.scope_key} 优先级 {rule.priority} "
            f"({'启用' if rule.enabled else '停用'}) 参数 {dict(rule.config) or '{}'}"
        )
        _render_guard_rules([rule])

    asyncio.run(_run())


@guard_app.command("enable")
def enable_guard_rule(rule_id: int = typer.Argument(...)) -> None:
    """启用一条规则。"""

    async def _run() -> None:
        try:
            rule = await DeliveryGuardService().set_rule_enabled(rule_id, enabled=True)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 规则 #{rule.local_id} {rule.rule_code} 已启用。")

    asyncio.run(_run())


@guard_app.command("disable")
def disable_guard_rule(rule_id: int = typer.Argument(...)) -> None:
    """停用一条规则。"""

    async def _run() -> None:
        try:
            rule = await DeliveryGuardService().set_rule_enabled(rule_id, enabled=False)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[yellow]OK[/yellow] 规则 #{rule.local_id} {rule.rule_code} 已停用。")

    asyncio.run(_run())


@guard_app.command("delete")
def delete_guard_rule(
    rule_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除一条规则。"""
    if not yes and not typer.confirm(f"确认删除守卫规则 #{rule_id}?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            await DeliveryGuardService().delete_rule(rule_id)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[red]OK[/red] 守卫规则 #{rule_id} 已删除。")

    asyncio.run(_run())


@guard_app.command("evaluate")
def evaluate_guard(
    account_id: str = typer.Option(..., "--account", "-a"),
    order_id: str = typer.Option(..., "--order-id", help="订单 id。"),
    item_id: str = typer.Option("", "--item-id"),
    buyer_id: str = typer.Option("", "--buyer-id"),
) -> None:
    """Dry Run:这条订单现在能不能自动发货。"""

    async def _run() -> None:
        outcome = await DeliveryGuardService().evaluate(
            GuardContext(
                account_id=account_id,
                order_id=order_id,
                item_id=item_id or None,
                buyer_id=buyer_id or None,
            )
        )
        color = "green" if outcome.allowed else "yellow"
        console.print(f"[{color}]{outcome.decision}[/{color}] {outcome.reason}")
        for result in outcome.results:
            console.print(f"  - {result.rule_code}: {result.verdict} ({result.reason})")

    asyncio.run(_run())


@blacklist_app.command("add")
def add_blacklist(
    buyer_id: str = typer.Option(..., "--buyer-id", help="买家标识。"),
    account_id: str = typer.Option("", "--account", "-a", help="限定账号;留空为全店。"),
    item_id: str = typer.Option("", "--item-id", help="限定商品;留空为全部商品。"),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """把买家加入黑名单。"""

    async def _run() -> None:
        try:
            entry = await DeliveryGuardService().add_blacklist(
                buyer_id=buyer_id,
                reason=reason,
                account_id=account_id or None,
                item_id=item_id or None,
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 黑名单 #{entry.local_id} 买家 {entry.buyer_id} 已加入。")

    asyncio.run(_run())


@blacklist_app.command("list")
def list_blacklist(
    buyer_id: str = typer.Option("", "--buyer-id"),
    account_id: str = typer.Option("", "--account", "-a"),
) -> None:
    """列出黑名单。"""

    async def _run() -> None:
        entries = await DeliveryGuardService().list_blacklist(
            buyer_id=buyer_id or None, account_id=account_id or None
        )
        if not entries:
            console.print("[dim]黑名单为空。[/dim]")
            return
        table = Table(title=f"买家黑名单 ({len(entries)})")
        for column in ("id", "buyer", "account", "item", "reason"):
            table.add_column(column)
        for entry in entries:
            table.add_row(
                str(entry.local_id),
                entry.buyer_id,
                entry.account_id or "-",
                entry.item_id or "-",
                entry.reason or "-",
            )
        console.print(table)

    asyncio.run(_run())


@blacklist_app.command("remove")
def remove_blacklist(
    entry_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """从黑名单移除。"""
    if not yes and not typer.confirm(f"确认移除黑名单 #{entry_id}?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            await DeliveryGuardService().delete_blacklist(entry_id)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[red]OK[/red] 黑名单 #{entry_id} 已移除。")

    asyncio.run(_run())


def _render_templates(templates: Sequence[DeliveryTemplate]) -> None:
    if not templates:
        console.print("[dim]暂无发货模板。[/dim]")
        return
    table = Table(title=f"发货模板 ({len(templates)})")
    for column in ("id", "name", "variables", "enabled", "content"):
        table.add_column(column, overflow="fold")
    for template in templates:
        table.add_row(
            str(template.local_id),
            template.name,
            "、".join(template.variables) or "(无)",
            "Y" if template.enabled else "N",
            template.content,
        )
    console.print(table)


@template_app.command("create")
def create_template(
    name: str = typer.Option(..., "--name", "-n"),
    content: str = typer.Option(..., "--content", "-c", help="模板正文,只用白名单变量。"),
    description: str = typer.Option("", "--desc"),
    disabled: bool = typer.Option(False, "--disabled", help="建好后先不启用。"),
) -> None:
    """新建可复用发货模板。"""

    async def _run() -> None:
        try:
            template = await DeliveryTemplateService().create(
                name=name, content=content, description=description or None, enabled=not disabled
            )
        except (ValueError, TemplateError) as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] {template.describe()} 变量 "
            f"{'、'.join(template.variables) or '(无)'}"
        )

    asyncio.run(_run())


@template_app.command("list")
def list_templates(
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的模板。"),
) -> None:
    """列出发货模板。"""

    async def _run() -> None:
        _render_templates(await DeliveryTemplateService().list_templates(only_enabled=only_enabled))

    asyncio.run(_run())


@template_app.command("show")
def show_template(template_id: int = typer.Argument(...)) -> None:
    """查看一个模板(含被多少条启用策略引用)。"""

    async def _run() -> None:
        service = DeliveryTemplateService()
        try:
            template = await service.get(template_id)
        except ValueError as exc:
            _fail(exc)
            return
        _render_templates([template])
        console.print(f"被启用策略引用:{await service.usage_count(template.name)} 条")
        if template.description:
            console.print(f"说明:{template.description}")

    asyncio.run(_run())


@template_app.command("set")
def set_template(
    template_id: int = typer.Argument(...),
    content: str = typer.Option("", "--content", "-c", help="替换模板正文。"),
    name: str = typer.Option("", "--name", "-n", help="改名。"),
    description: str = typer.Option("", "--desc"),
) -> None:
    """修改模板正文或名称。"""

    async def _run() -> None:
        try:
            template = await DeliveryTemplateService().update(
                template_id,
                name=name or None,
                content=content or None,
                description=description or None,
            )
        except (ValueError, TemplateError) as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] {template.describe()} 已更新。")

    asyncio.run(_run())


@template_app.command("enable")
def enable_template(template_id: int = typer.Argument(...)) -> None:
    """启用模板。"""

    async def _run() -> None:
        try:
            template = await DeliveryTemplateService().set_enabled(template_id, enabled=True)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] {template.describe()} 已启用。")

    asyncio.run(_run())


@template_app.command("disable")
def disable_template(template_id: int = typer.Argument(...)) -> None:
    """停用模板;停用后引用它的策略无法发货。"""

    async def _run() -> None:
        try:
            template = await DeliveryTemplateService().set_enabled(template_id, enabled=False)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[yellow]OK[/yellow] {template.describe()} 已停用。")

    asyncio.run(_run())


@template_app.command("delete")
def delete_template(
    template_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除模板;仍被启用策略引用时拒绝删除。"""
    if not yes:
        console.print("[red]删除模板不可恢复,请加 --yes。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            await DeliveryTemplateService().delete(template_id)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 模板 #{template_id} 已删除。")

    asyncio.run(_run())


api_app = typer.Typer(help="外部发卡 API:结构配置(密钥只存引用)。")
api_secret_app = typer.Typer(help="发货 API 密钥:加密写入 SecretVault,永不回显明文。")
app.add_typer(api_app, name="api")
api_app.add_typer(api_secret_app, name="secret")


def _api_secret_vault() -> SqlAlchemySecretVault:
    return SqlAlchemySecretVault(FernetKeyring.from_environment())


@api_secret_app.command("add")
def add_api_secret(
    value: str = typer.Option(..., prompt="密钥值", hide_input=True),
    label: str = typer.Option("", "--label", help="非敏感用途标签。"),
) -> None:
    """创建一个加密密钥,输出可放进 {{secret:引用}} 的不透明引用。"""

    async def _run() -> None:
        try:
            ref = await _api_secret_vault().put(
                SecretValue(value),
                metadata=SecretMetadata(
                    {"purpose": "delivery_api", "label": label.strip() or "delivery_api"}
                ),
            )
        except Exception as exc:
            console.print(f"[red]保存发货 API 密钥失败:{type(exc).__name__}。[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] 密钥已加密保存;模板引用: [cyan]{{{{secret:{ref.value}}}}}[/cyan]"
        )

    asyncio.run(_run())


@api_secret_app.command("status")
def api_secret_status(ref: str = typer.Argument(..., help="SecretVault 不透明引用。")) -> None:
    """确认引用是否存在,不读取或显示密钥。"""

    async def _run() -> None:
        try:
            configured = await _api_secret_vault().configured(SecretRef(ref))
        except Exception as exc:
            console.print(f"[red]检查发货 API 密钥失败:{type(exc).__name__}。[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"configured={'Y' if configured else 'N'} ref={ref}")

    asyncio.run(_run())


@api_secret_app.command("rotate")
def rotate_api_secret(
    ref: str = typer.Argument(..., help="现有 SecretVault 不透明引用。"),
    value: str = typer.Option(..., prompt="新密钥值", hide_input=True),
) -> None:
    """原引用不变地轮换密钥,现有发货模板无需修改。"""

    async def _run() -> None:
        try:
            await _api_secret_vault().replace(SecretRef(ref), SecretValue(value))
        except Exception as exc:
            console.print(f"[red]轮换发货 API 密钥失败:{type(exc).__name__}。[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 密钥 {ref} 已轮换,明文未回显。")

    asyncio.run(_run())


def _parse_headers(values: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            msg = f"--header 需要 名称=值 形式: {item}"
            raise ValueError(msg)
        headers[name.strip()] = value
    return headers


def _render_api_configs(configs: Sequence[ApiDeliveryConfig]) -> None:
    if not configs:
        console.print("[dim]暂无发卡 API 配置。[/dim]")
        return
    table = Table(title=f"发卡 API 配置 ({len(configs)})")
    for column in ("id", "name", "method", "url", "response", "secrets", "enabled"):
        table.add_column(column)
    for config in configs:
        table.add_row(
            str(config.local_id),
            config.name,
            config.method,
            config.url,
            config.response_path,
            ",".join(config.secret_refs) or "-",
            "Y" if config.enabled else "N",
        )
    console.print(table)


@api_app.command("list")
def list_api_configs(
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的配置。"),
) -> None:
    """列出外部发卡 API 配置(不显示密钥)。"""

    async def _run() -> None:
        configs = await ApiConfigService().list_configs(only_enabled=only_enabled)
        _render_api_configs(configs)

    asyncio.run(_run())


@api_app.command("test")
def probe_api_config(
    ref: str = typer.Argument(..., help="API 配置 id 或名称。"),
    order_id: str = typer.Option("TEST-ORDER", "--order-id", help="样例订单号。"),
    quantity: str = typer.Option("1", "--quantity", help="样例数量。"),
) -> None:
    """试调用一次发卡 API:只验证能不能取到卡,不预留库存、不给买家发消息。

    取到的内容属于真实卡密资源,终端只显示掩码预览与长度。
    """

    async def _run() -> None:
        try:
            result = await ApiConfigService().test_fetch(
                ref, variables={"order_id": order_id, "quantity": quantity}
            )
        except (ValueError, ApiConfigError) as exc:
            _fail(exc)
            return
        if result.content is None:
            console.print(
                f"[red]取卡失败[/red] HTTP {result.status_code or '-'} "
                f"| {'结果不明' if result.dispatched else '未发出'} | {result.error or '-'}"
            )
            raise typer.Exit(code=1)
        console.print(
            f"[green]OK[/green] 取到卡密 预览 {masked_preview(result.content)} "
            f"| 长度 {len(result.content)} | HTTP {result.status_code}"
        )

    asyncio.run(_run())


@api_app.command("show")
def show_api_config(config_id: int = typer.Argument(...)) -> None:
    """查看一条配置(含模板与所需密钥引用)。"""

    async def _run() -> None:
        try:
            config = await ApiConfigService().get(config_id)
        except ValueError as exc:
            _fail(exc)
            return
        _render_api_configs([config])
        console.print(f"请求体模板:{config.body}")
        console.print(f"业务变量:{'、'.join(config.variables) or '(无)'}")
        console.print(f"密钥引用:{'、'.join(config.secret_refs) or '(无)'}")

    asyncio.run(_run())


@api_app.command("set")
def set_api_config(  # noqa: PLR0917 (CLI option surface)
    name: str = typer.Option(..., "--name", "-n", help="配置名(同时用作库存池的 api_config_ref)。"),
    url: str = typer.Option(..., "--url", help="https 接口地址。"),
    method: str = typer.Option("POST", "--method", help="GET / POST / PUT。"),
    body: str = typer.Option("{}", "--body", help="参数对象模板:POST/PUT 当请求体,GET 当查询参数。"),
    response_path: str = typer.Option("card", "--response-path", help="响应取值路径,如 data.card"),
    timeout_seconds: float = typer.Option(10.0, "--timeout", min=0.5, max=60.0),
    header: list[str] = typer.Option([], "--header", help="请求头,形如 名称=值,可重复"),  # noqa: B008
    description: str = typer.Option("", "--desc"),
    enabled: bool = typer.Option(True, "--enabled/--disabled"),
) -> None:
    """新建或更新一条发卡 API 配置。"""

    async def _run() -> None:
        service = ApiConfigService()
        try:
            headers = _parse_headers(header)
            existing = [config for config in await service.list_configs() if config.name == name]
            if existing and existing[0].local_id is not None:
                config = await service.update(
                    existing[0].local_id,
                    url=url,
                    method=method,
                    headers=headers,
                    body=body,
                    response_path=response_path,
                    timeout_seconds=timeout_seconds,
                    enabled=enabled,
                    description=description or None,
                )
                action = "已更新"
            else:
                config = await service.create(
                    name=name,
                    url=url,
                    method=method,
                    headers=headers,
                    body=body,
                    response_path=response_path,
                    timeout_seconds=timeout_seconds,
                    enabled=enabled,
                    description=description or None,
                )
                action = "已创建"
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 配置 #{config.local_id} {action}:{config.describe()} "
            f"密钥引用 {'、'.join(config.secret_refs) or '(无)'}"
        )

    asyncio.run(_run())


@api_app.command("enable")
def enable_api_config(config_id: int = typer.Argument(...)) -> None:
    """启用配置。"""

    async def _run() -> None:
        try:
            config = await ApiConfigService().set_enabled(config_id, enabled=True)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 配置 #{config.local_id} {config.name} 已启用。")

    asyncio.run(_run())


@api_app.command("disable")
def disable_api_config(config_id: int = typer.Argument(...)) -> None:
    """停用配置。"""

    async def _run() -> None:
        try:
            config = await ApiConfigService().set_enabled(config_id, enabled=False)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[yellow]OK[/yellow] 配置 #{config.local_id} {config.name} 已停用。")

    asyncio.run(_run())


@api_app.command("delete")
def delete_api_config(
    config_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除配置(库存池的引用需要一并清理)。"""
    if not yes and not typer.confirm(f"确认删除发卡 API 配置 #{config_id}?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            await ApiConfigService().delete(config_id)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[red]OK[/red] 发卡 API 配置 #{config_id} 已删除。")

    asyncio.run(_run())
