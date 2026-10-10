"""回复投递记录:脱敏查看与重置(对照 Helper 的 default-replies/clear-records)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.message import rules as domain_rules

runner = CliRunner()


@pytest.fixture
async def records_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "records.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_reply(rule_id: int, text: str) -> None:
    await domain_rules.record_reply_log(
        account_id="acc-1", message_id=None, rule_id=rule_id, sent_text=text, success=True
    )


async def test_log_records_mask_the_text(records_db: Path) -> None:
    rule = await domain_rules.create_rule(
        "默认回复", "default", "", "你好呀,这是一条比较长的默认回复文案", account_id="acc-1"
    )
    await _seed_reply(rule.id, "你好呀,这是一条比较长的默认回复文案")

    rows = await domain_rules.list_reply_logs("acc-1")

    assert len(rows) == 1
    assert rows[0].rule_name == "默认回复"
    assert rows[0].text_length > 8
    # 预览只给前 8 位,完整正文不该出现在这条只读投影里。
    assert rows[0].text_preview == "你好呀,这是一条…"
    assert "默认回复文案" not in (rows[0].text_preview or "")


async def test_log_records_reject_an_unknown_account(records_db: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        await domain_rules.list_reply_logs("missing")


async def test_clear_records_is_scoped_and_audited(records_db: Path) -> None:
    first = await domain_rules.create_rule("规则一", "keyword", "在吗", "在的", account_id="acc-1")
    second = await domain_rules.create_rule("规则二", "keyword", "价格", "9.9", account_id="acc-1")
    await _seed_reply(first.id, "在的")
    await _seed_reply(second.id, "9.9")

    removed = await domain_rules.clear_reply_logs("acc-1", rule_id=first.id)

    assert removed == 1
    remaining = await domain_rules.list_reply_logs("acc-1")
    assert [row.rule_id for row in remaining] == [second.id]
    async with db_mod.async_session_factory() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "reply_records_cleared")
            )
        ).scalar_one()
    assert audit.actor == "cli"
    assert audit.target == "acc-1"
    assert audit.params == {"rule_id": first.id, "removed": 1}


def test_cli_exposes_logs_and_reset_records(records_db: Path) -> None:
    assert runner.invoke(app, ["rule", "logs", "-a", "acc-1"]).exit_code == 0

    # 没有 --yes 时只提示、不执行;真正清空需要显式确认。
    refused = runner.invoke(app, ["rule", "reset-records", "-a", "acc-1"])
    assert refused.exit_code == 1
    assert "--yes" in refused.output

    confirmed = runner.invoke(app, ["rule", "reset-records", "-a", "acc-1", "--yes"])
    assert confirmed.exit_code == 0
    assert "已清空" in confirmed.output
