"""与 RuntimeDaemon 同进程的容器健康端点。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import update

from xianyu_agent.application.health.runtime_health import observe_daemon
from xianyu_agent.db import DaemonInstance, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, daemon as daemon_domain


@dataclass(frozen=True)
class HealthReport:
    status: str
    instance_id: str
    database_writable: bool
    daemon: str
    heartbeat_age_s: float | None
    owned_accounts: int
    desired_accounts: int
    owned_drifted_accounts: int


async def probe_health(instance_id: str) -> HealthReport:
    """验证数据库可写、本实例心跳和所持账号摘要。"""
    database_writable = False
    try:
        async with get_async_session() as session:
            result = await session.execute(
                update(DaemonInstance)
                .where(DaemonInstance.instance_id == instance_id)
                # 无值变化的 UPDATE 在事务回滚前已足以验证写权限。
                .values(last_heartbeat_at=DaemonInstance.last_heartbeat_at)
                .execution_options(synchronize_session=False)
            )
            database_writable = bool(result.rowcount)
            await session.rollback()
    except Exception:
        database_writable = False

    row = await daemon_domain.get_instance(instance_id)
    health = observe_daemon(row)
    accounts = await domain_accounts.list_accounts()
    owned = [account for account in accounts if account.owner_instance_id == instance_id]
    desired = [
        account for account in accounts if account.enabled and account.desired_state == "running"
    ]
    statuses = {status.account_id: status for status in await domain_accounts.worker_statuses()}
    drifted = sum(
        1
        for account in owned
        if account.desired_state == "running"
        and (
            (status := statuses.get(account.id)) is None
            or str(status.status).lower() not in {"online", "connected"}
        )
    )
    ok = database_writable and health.healthy
    return HealthReport(
        status="healthy" if ok else "unhealthy",
        instance_id=instance_id,
        database_writable=database_writable,
        daemon=health.observed,
        heartbeat_age_s=health.heartbeat_age_s,
        owned_accounts=len(owned),
        desired_accounts=len(desired),
        owned_drifted_accounts=drifted,
    )


def create_health_app(instance_id: str) -> FastAPI:
    app = FastAPI(
        title="xianyu-agent health",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        report = await probe_health(instance_id)
        return JSONResponse(
            asdict(report),
            status_code=200 if report.status == "healthy" else 503,
        )

    return app
