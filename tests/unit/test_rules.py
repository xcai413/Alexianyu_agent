"""Unit tests for reply rule domain (CRUD + matching semantics)."""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, rules as domain_rules


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "rules.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_rule_crud_and_matching(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)

    kw = await domain_rules.create_rule(
        "价格关键词",
        "keyword",
        "多少钱",
        "亲,价格是 9.9 元哦",
        account_id="acc-1",
        priority=10,
    )
    rg = await domain_rules.create_rule(
        "砍价正则",
        "regex",
        r"(\d+)\s*元.*便宜",
        "亲,已是最低价啦",
        account_id="acc-1",
        priority=20,
    )
    df = await domain_rules.create_rule(
        "默认回复",
        "default",
        "",
        "您好,请问需要什么帮助?",
        account_id=None,
        priority=999,
    )

    # list + scope
    assert len(await domain_rules.list_rules(account_id="acc-1")) == 2
    assert len(await domain_rules.list_rules(account_id="acc-2")) == 0
    assert len(await domain_rules.list_rules()) == 3

    # keyword match (case-insensitive)
    hits = await domain_rules.match_for_account("acc-1", "请问这个多少钱?")
    assert [r.id for r in hits] == [kw.id, df.id]

    # regex match
    hits = await domain_rules.match_for_account("acc-1", "20 元能便宜点吗")
    assert [r.id for r in hits] == [rg.id, df.id]

    # default only
    hits = await domain_rules.match_for_account("acc-1", "你好在吗")
    assert [r.id for r in hits] == [df.id]

    # disabled rules never match
    await domain_rules.set_rule_enabled(kw.id, False)
    hits = await domain_rules.match_for_account("acc-1", "这个多少钱")
    assert kw.id not in [r.id for r in hits]

    # hit count
    await domain_rules.record_hit(df.id)
    rule = await domain_rules.get_rule(df.id)
    assert rule.hit_count == 1
    assert rule.last_hit_at is not None

    # delete
    assert await domain_rules.delete_rule(df.id) is True
    assert await domain_rules.get_rule(df.id) is None
    assert await domain_rules.delete_rule(9999) is False


@pytest.mark.asyncio
async def test_rule_validation_and_reply_log(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)

    with pytest.raises(ValueError, match="type_"):
        await domain_rules.create_rule("bad", "unknown-type", "x", "y")
    with pytest.raises(ValueError, match="不存在"):
        await domain_rules.create_rule("bad", "keyword", "x", "y", account_id="ghost")

    rule = await domain_rules.create_rule("k", "keyword", "在吗", "在的")
    log = await domain_rules.record_reply_log(
        account_id="acc-1",
        message_id=None,
        rule_id=rule.id,
        sent_text="在的",
        success=True,
        source="rule",
    )
    assert log is not None
    assert log.success is True
    logs = await domain_rules.recent_reply_logs()
    assert len(logs) == 1
    assert logs[0].sent_text == "在的"

    # unknown account -> no log
    assert (
        await domain_rules.record_reply_log(
            account_id="ghost", message_id=None, rule_id=rule.id, sent_text="x", success=False
        )
        is None
    )
