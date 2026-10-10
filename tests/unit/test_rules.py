"""Unit tests for reply rule domain (CRUD + matching semantics)."""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import (
    accounts as domain_accounts,
    items as domain_items,
    rules as domain_rules,
)
from xianyu_agent.protocol.items_client import RemoteItem


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

    assert (
        await domain_rules.record_reply_log(
            account_id="ghost", message_id=None, rule_id=rule.id, sent_text="x", success=False
        )
        is None
    )


@pytest.mark.asyncio
async def test_item_rule_wins_only_for_its_account_item(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [RemoteItem("item-a", "商品 A", "9.9", "1", None, None, None, None, {})],
    )
    await domain_items.apply_on_sale_snapshot(
        "acc-2",
        [RemoteItem("item-b", "商品 B", "9.9", "1", None, None, None, None, {})],
    )
    item = (await domain_items.list_items("acc-1"))[0]
    global_rule = await domain_rules.create_rule("全局", "keyword", "在吗", "全局")
    account_rule = await domain_rules.create_rule(
        "账号", "keyword", "在吗", "账号", account_id="acc-1"
    )
    item_rule = await domain_rules.create_rule("商品", "keyword", "在吗", "商品", item_pk=item.id)

    hits = await domain_rules.match_for_account("acc-1", "在吗", item_id="item-a")
    assert [rule.id for rule in hits] == [item_rule.id, account_rule.id, global_rule.id]
    hits = await domain_rules.match_for_account("acc-1", "在吗", item_id="unknown")
    assert [rule.id for rule in hits] == [account_rule.id, global_rule.id]
    hits = await domain_rules.match_for_account("acc-2", "在吗", item_id="item-b")
    assert [rule.id for rule in hits] == [global_rule.id]

    with pytest.raises(ValueError, match="不属于账号"):
        await domain_rules.create_rule(
            "串号", "keyword", "在吗", "x", account_id="acc-2", item_pk=item.id
        )


@pytest.mark.asyncio
async def test_keyword_precedes_same_priority_default_rule(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    default = await domain_rules.create_rule("默认", "default", "", "默认", account_id="acc-1")
    keyword = await domain_rules.create_rule(
        "关键词", "keyword", "在吗", "关键词", account_id="acc-1"
    )
    hits = await domain_rules.match_for_account("acc-1", "在吗")
    assert [rule.id for rule in hits] == [keyword.id, default.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", [None, "", "unknown", "foreign-item"])
@pytest.mark.parametrize(
    ("rule_type", "pattern"), [("keyword", "在吗"), ("regex", "在.*吗"), ("default", "")]
)
async def test_missing_item_never_matches_other_account_rules(
    clean_db, item_id: str | None, rule_type: str, pattern: str
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    await domain_items.apply_on_sale_snapshot(
        "acc-2",
        [RemoteItem("foreign-item", "其他账号商品", "9.9", "1", None, None, None, None, {})],
    )
    foreign = await domain_rules.create_rule(
        "其他账号", rule_type, pattern, "其他账号模板", account_id="acc-2", priority=1
    )
    own = await domain_rules.create_rule(
        "当前账号", rule_type, pattern, "当前账号模板", account_id="acc-1", priority=100
    )
    global_rule = await domain_rules.create_rule("全局", rule_type, pattern, "全局模板")

    matches = await domain_rules.match_for_account("acc-1", "在吗", item_id=item_id)
    assert [rule.id for rule in matches] == [own.id, global_rule.id]
    foreign_matches = await domain_rules.match_for_account("acc-2", "在吗", item_id=item_id)
    assert [rule.id for rule in foreign_matches] == [foreign.id, global_rule.id]
    assert await domain_rules.match_for_account("missing-account", "在吗", item_id=item_id) == []


@pytest.mark.asyncio
async def test_same_platform_item_id_keeps_each_accounts_item_rules_isolated(clean_db) -> None:
    expected = {}
    global_rule = await domain_rules.create_rule("全局", "keyword", "在吗", "全局")
    for account_id in ("acc-1", "acc-2"):
        await domain_accounts.create_account(account_id, enabled=True)
        await domain_items.apply_on_sale_snapshot(
            account_id,
            [RemoteItem("shared-item", "商品", "9.9", "1", None, None, None, None, {})],
        )
        item = (await domain_items.list_items(account_id))[0]
        account_rule = await domain_rules.create_rule(
            "账号规则", "keyword", "在吗", account_id, account_id=account_id
        )
        item_rule = await domain_rules.create_rule(
            "商品规则", "keyword", "在吗", account_id, item_pk=item.id
        )
        expected[account_id] = [item_rule.id, account_rule.id, global_rule.id]
    for account_id, rule_ids in expected.items():
        matches = await domain_rules.match_for_account(account_id, "在吗", item_id="shared-item")
        assert [rule.id for rule in matches] == rule_ids
