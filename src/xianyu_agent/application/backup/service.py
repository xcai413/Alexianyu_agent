"""运营配置的导出与导入(参考 super-butler 的备份功能)。

边界(与参考实现的关键差异):

- **不导出任何凭据**:Cookie、IM Token、发货 API 密钥、AI Key 都不进备份文件。它们要么在加密列里,
  要么在 SecretVault 里;备份只带结构、引用名与文案。换机器时凭据必须重新扫码或重新填写。
- **不导出真实校准批准记录**:擦亮/求花的校准是人工对真实平台动作的确认,换环境必须重新登记,
  否则一次导入就会绕过整条门禁。因此导入后这些动作仍然是停用状态。
- **导入是幂等的**:同名对象已存在就跳过,不覆盖、不删除,重复导入不会破坏现有配置。
- 卡密正文属于敏感数据,默认不导出;需要时显式加 `--include-cards`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xianyu_agent.application.delivery import (
    ApiConfigService,
    DeliveryPolicyService,
    DeliveryTemplateService,
)
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryQueryService,
    InventoryService,
)
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, rules as domain_rules
from xianyu_agent.infrastructure.database.models import Account as AccountRow
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyInventoryRepository

SCHEMA_VERSION = 1
MAX_UNITS_PER_POOL = 50_000


@dataclass(slots=True)
class BackupReport:
    """一次导入的结果:每类对象各创建/跳过了多少。"""

    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def add(self, section: str, *, created: bool) -> None:
        bucket = self.created if created else self.skipped
        bucket[section] = bucket.get(section, 0) + 1

    @property
    def total_created(self) -> int:
        return sum(self.created.values())

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped.values())


class BackupService:
    """Export and import the operational configuration as a JSON document."""

    async def export_document(
        self,
        *,
        include_cards: bool = False,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Build one backup document; never contains credentials or calibration records."""
        accounts = await domain_accounts.list_accounts()
        selected = [row for row in accounts if account_id is None or row.account_id == account_id]
        rules = await domain_rules.list_rules(account_id=account_id if account_id else None)
        api_configs = await ApiConfigService().list_configs()
        templates = await DeliveryTemplateService().list_templates()
        policies = await DeliveryPolicyService().list(account_id=account_id or None)
        pools = await InventoryQueryService().list_pools()

        pool_documents: list[dict[str, Any]] = []
        async with db_mod.async_session_factory() as session:
            repository = SqlAlchemyInventoryRepository(session)
            for view in pools:
                pool = view.pool
                units: list[str] = []
                if include_cards and pool.is_consumable and pool.local_id is not None:
                    rows = await repository.list_units(pool.local_id, limit=MAX_UNITS_PER_POOL)
                    units = [unit.content for unit in rows if unit.state == "available"]
                pool_documents.append(
                    {
                        "name": pool.name,
                        "type": pool.type,
                        "description": pool.description,
                        "delay_seconds": pool.delay_seconds,
                        "account_scope": pool.account_scope,
                        "item_scope": pool.item_scope,
                        "api_config_ref": pool.api_config_ref,
                        "content": pool.content,
                        "image_url": pool.image_url,
                        "enabled": pool.enabled,
                        # 只带可用卡密:已预留/已核销的单元属于历史事实,不进备份。
                        "units": units,
                    }
                )

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "includes_cards": include_cards,
            "accounts": [
                {
                    "account_id": row.account_id,
                    "nickname": row.nickname,
                    "remark": row.remark,
                    "enabled": row.enabled,
                }
                for row in selected
            ],
            "reply_rules": [
                {
                    "name": row.name,
                    "type": row.type,
                    "pattern": row.pattern,
                    "reply_text": row.reply_text,
                    "reply_image_url": row.reply_image_url,
                    "priority": row.priority,
                    "enabled": row.enabled,
                    "reply_once": row.reply_once,
                    "account_id": await self._account_of_rule(row),
                }
                for row in rules
            ],
            "api_configs": [
                {
                    "name": config.name,
                    "url": config.url,
                    "method": config.method,
                    "headers": dict(config.headers),
                    "body": config.body,
                    "response_path": config.response_path,
                    "timeout_seconds": config.timeout_seconds,
                    "enabled": config.enabled,
                    "description": config.description,
                }
                for config in api_configs
            ],
            "delivery_templates": [
                {
                    "name": template.name,
                    "content": template.content,
                    "description": template.description,
                    "enabled": template.enabled,
                }
                for template in templates
            ],
            "inventory_pools": pool_documents,
            "delivery_policies": [
                {
                    "name": policy.name,
                    "pool_name": await self._pool_name(policy.inventory_pool_id, pools),
                    "account_id": policy.account_id,
                    "item_id": policy.item_id,
                    "delay_seconds": policy.delay_seconds,
                    "delivery_mode": policy.delivery_mode,
                    "template": policy.template,
                    "template_ref": policy.template_ref,
                    "platform_confirm": policy.platform_confirm,
                    "priority": policy.priority,
                    "enabled": policy.enabled,
                    "description": policy.description,
                }
                for policy in policies
            ],
        }

    async def import_document(
        self,
        document: Mapping[str, Any],
        *,
        dry_run: bool = False,
    ) -> BackupReport:
        """Restore the configuration; existing names are skipped, never overwritten."""
        version = document.get("schema_version")
        if version != SCHEMA_VERSION:
            msg = f"备份文件版本 {version!r} 不受支持,当前是 {SCHEMA_VERSION}"
            raise ValueError(msg)
        report = BackupReport()
        await self._import_accounts(document.get("accounts") or (), report, dry_run=dry_run)
        await self._import_api_configs(document.get("api_configs") or (), report, dry_run=dry_run)
        await self._import_templates(
            document.get("delivery_templates") or (), report, dry_run=dry_run
        )
        pool_ids = await self._import_pools(
            document.get("inventory_pools") or (), report, dry_run=dry_run
        )
        await self._import_policies(
            document.get("delivery_policies") or (), pool_ids, report, dry_run=dry_run
        )
        await self._import_rules(document.get("reply_rules") or (), report, dry_run=dry_run)
        return report

    async def _import_accounts(
        self, rows: Sequence[Mapping[str, Any]], report: BackupReport, *, dry_run: bool
    ) -> None:
        existing = {row.account_id for row in await domain_accounts.list_accounts()}
        for row in rows:
            account_id = str(row.get("account_id") or "").strip()
            if not account_id:
                continue
            if account_id in existing:
                report.add("accounts", created=False)
                continue
            if not dry_run:
                await domain_accounts.create_account(
                    account_id,
                    nickname=row.get("nickname"),
                    remark=row.get("remark"),
                    enabled=bool(row.get("enabled", True)),
                )
            report.add("accounts", created=True)

    async def _import_api_configs(
        self, rows: Sequence[Mapping[str, Any]], report: BackupReport, *, dry_run: bool
    ) -> None:
        service = ApiConfigService()
        existing = {config.name for config in await service.list_configs()}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            if name in existing:
                report.add("api_configs", created=False)
                continue
            if not dry_run:
                await service.create(
                    name=name,
                    url=str(row.get("url") or ""),
                    method=str(row.get("method") or "POST"),
                    headers=dict(row.get("headers") or {}),
                    body=str(row.get("body") or "{}"),
                    response_path=str(row.get("response_path") or "card"),
                    timeout_seconds=float(row.get("timeout_seconds") or 10.0),
                    enabled=bool(row.get("enabled", True)),
                    description=row.get("description"),
                )
            existing.add(name)
            report.add("api_configs", created=True)

    async def _import_templates(
        self, rows: Sequence[Mapping[str, Any]], report: BackupReport, *, dry_run: bool
    ) -> None:
        service = DeliveryTemplateService()
        existing = {template.name for template in await service.list_templates()}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            if name in existing:
                report.add("delivery_templates", created=False)
                continue
            if not dry_run:
                await service.create(
                    name=name,
                    content=str(row.get("content") or ""),
                    description=row.get("description"),
                    enabled=bool(row.get("enabled", True)),
                )
            existing.add(name)
            report.add("delivery_templates", created=True)

    async def _import_pools(
        self, rows: Sequence[Mapping[str, Any]], report: BackupReport, *, dry_run: bool
    ) -> dict[str, int]:
        service = InventoryService()
        importer = InventoryImportService()
        by_name = {
            view.pool.name: view.pool.local_id or 0
            for view in await InventoryQueryService().list_pools()
        }
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            if name in by_name:
                report.add("inventory_pools", created=False)
            elif not dry_run:
                pool = await service.create_pool(
                    name=name,
                    type_=str(row.get("type") or "data"),
                    description=row.get("description"),
                    enabled=bool(row.get("enabled", True)),
                    delay_seconds=int(row.get("delay_seconds") or 0),
                    account_scope=row.get("account_scope"),
                    item_scope=row.get("item_scope"),
                    api_config_ref=row.get("api_config_ref"),
                    content=row.get("content"),
                    image_url=row.get("image_url"),
                )
                by_name[name] = pool.local_id or 0
                report.add("inventory_pools", created=True)
            units = row.get("units") or ()
            pool_id = by_name.get(name)
            if units and pool_id and not dry_run:
                await importer.import_units(pool_id, [str(unit) for unit in units])
        return by_name

    async def _import_policies(
        self,
        rows: Sequence[Mapping[str, Any]],
        pool_ids: Mapping[str, int],
        report: BackupReport,
        *,
        dry_run: bool,
    ) -> None:
        service = DeliveryPolicyService()
        existing = {(policy.name, policy.scope_key()) for policy in await service.list()}
        for row in rows:
            name = str(row.get("name") or "").strip()
            pool_name = str(row.get("pool_name") or "").strip()
            pool_id = pool_ids.get(pool_name)
            if not name or not pool_id:
                # 池名对不上时整条跳过:把策略挂到错误的池上比不导入更危险。
                report.add("delivery_policies", created=False)
                continue
            if not dry_run:
                policy = await service.create(
                    name=name,
                    inventory_pool_id=pool_id,
                    account_id=row.get("account_id"),
                    item_id=row.get("item_id"),
                    delay_seconds=int(row.get("delay_seconds") or 0),
                    delivery_mode=row.get("delivery_mode"),
                    template=row.get("template"),
                    template_ref=row.get("template_ref"),
                    platform_confirm=bool(row.get("platform_confirm", False)),
                    priority=int(row.get("priority") or 0),
                    enabled=bool(row.get("enabled", True)),
                    description=row.get("description"),
                )
                key = (policy.name, policy.scope_key())
            else:
                key = (name, "")
            created = key not in existing
            existing.add(key)
            report.add("delivery_policies", created=created)

    async def _import_rules(
        self, rows: Sequence[Mapping[str, Any]], report: BackupReport, *, dry_run: bool
    ) -> None:
        existing = {(row.name, row.pattern) for row in await domain_rules.list_rules()}
        for row in rows:
            name = str(row.get("name") or "").strip()
            pattern = str(row.get("pattern") or "")
            if not name:
                continue
            if (name, pattern) in existing:
                report.add("reply_rules", created=False)
                continue
            if not dry_run:
                await domain_rules.create_rule(
                    name,
                    str(row.get("type") or "keyword"),
                    pattern,
                    str(row.get("reply_text") or ""),
                    account_id=row.get("account_id"),
                    priority=int(row.get("priority") or 100),
                    enabled=bool(row.get("enabled", True)),
                    reply_once=bool(row.get("reply_once", False)),
                    reply_image_url=row.get("reply_image_url"),
                )
            existing.add((name, pattern))
            report.add("reply_rules", created=True)

    @staticmethod
    async def _account_of_rule(rule: Any) -> str | None:
        if getattr(rule, "account_id", None) is None:
            return None
        async with get_async_session() as session:
            account = await session.get(AccountRow, rule.account_id)
        return None if account is None else account.account_id

    @staticmethod
    async def _pool_name(pool_id: int, views: Sequence[Any]) -> str | None:
        for view in views:
            if view.pool.local_id == pool_id:
                return view.pool.name
        return None
