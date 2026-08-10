"""Card inventory domain: CRUD + atomic consumption."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, text as sa_text

from xianyu_agent.db import Account, Card, CardConsumption, get_async_session
from xianyu_agent.db.models import CardType


def _split_content(content: str | None) -> list[str]:
    if not content:
        return []
    return [line.strip() for line in content.splitlines() if line.strip()]


def _compute_counts(type_: str, content: str | None) -> tuple[int, int]:
    lines = _split_content(content)
    if type_ == CardType.TEXT.value:
        total = len(lines)
        return total, total
    # non-text card types hold a single payload
    return (1, 1) if content else (0, 0)


async def create_card(
    account_id: str,
    name: str,
    content: str | None = None,
    *,
    type_: str = CardType.TEXT.value,
    unit_price: float = 0.0,
    description: str | None = None,
    enabled: bool = True,
) -> Card:
    """Create a card. For text cards, each non-empty line is one code."""
    if type_ not in {t.value for t in CardType}:
        msg = f"type_ 必须是 {[t.value for t in CardType]},got {type_!r}"
        raise ValueError(msg)
    total, remaining = _compute_counts(type_, content)
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            msg = f"账号 {account_id} 不存在"
            raise ValueError(msg)
        row = Card(
            account_id=account.id,
            name=name,
            type=type_,
            content=content,
            description=description,
            total=total,
            remaining=remaining,
            unit_price=unit_price,
            enabled=enabled,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def restock(card_id: int, content: str) -> Card | None:
    """Append more codes to a card. Returns None if card missing."""
    lines = _split_content(content)
    if not lines:
        return None
    async with get_async_session() as session:
        row = (
            await session.execute(select(Card).where(Card.id == card_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return None
        existing = row.content or ""
        row.content = (existing + "\n" if existing else "") + "\n".join(lines)
        row.total += len(lines)
        row.remaining += len(lines)
        await session.commit()
        await session.refresh(row)
        return row


async def list_cards(
    *, account_id: str | None = None, only_enabled: bool = False
) -> Sequence[Card]:
    async with get_async_session() as session:
        stmt = select(Card)
        if account_id is not None:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return []
            stmt = stmt.where(Card.account_id == account.id)
        if only_enabled:
            stmt = stmt.where(Card.enabled.is_(True))
        stmt = stmt.order_by(Card.id.asc())
        return list((await session.execute(stmt)).scalars().all())


async def get_card(card_id: int) -> Card | None:
    async with get_async_session() as session:
        return (
            await session.execute(select(Card).where(Card.id == card_id).limit(1))
        ).scalar_one_or_none()


async def set_card_enabled(card_id: int, enabled: bool) -> bool:
    async with get_async_session() as session:
        row = (
            await session.execute(select(Card).where(Card.id == card_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.enabled = enabled
        await session.commit()
        return True


async def delete_card(card_id: int) -> bool:
    async with get_async_session() as session:
        row = (
            await session.execute(select(Card).where(Card.id == card_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def consume_card(card_id: int, order_id: int | None) -> str | None:
    """Atomically take one unused code from the card.

    Serialization: SQLite single-writer plus a conditional decrement guard.
    Returns the code, or None when out of stock / disabled / no code left.
    """
    async with get_async_session() as session:
        row = (
            await session.execute(select(Card).where(Card.id == card_id).limit(1))
        ).scalar_one_or_none()
        if row is None or not row.enabled or row.remaining <= 0 or not row.content:
            return None
        used_stmt = select(CardConsumption.content).where(CardConsumption.card_id == card_id)
        used = set((await session.execute(used_stmt)).scalars().all())
        code = None
        for line in _split_content(row.content):
            if line not in used:
                code = line
                break
        if code is None:
            return None
        # Conditional decrement as a DB-level guard against double spend.
        result = await session.execute(
            sa_text("UPDATE cards SET remaining = remaining - 1 WHERE id = :id AND remaining > 0"),
            {"id": card_id},
        )
        if result.rowcount != 1:  # pragma: no cover - race guard; used-check short-circuits
            return None
        session.add(
            CardConsumption(
                card_id=card_id,
                order_id=order_id,
                content=code,
                status="success",
            )
        )
        await session.commit()
        return code


async def recent_consumptions(*, limit: int = 20) -> Sequence[CardConsumption]:
    async with get_async_session() as session:
        stmt = select(CardConsumption).order_by(CardConsumption.consumed_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())
