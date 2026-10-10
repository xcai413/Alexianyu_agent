"""批量铺货的草稿、校验与批次状态机(Issue #4 M13)。

草稿是一行导入数据的规范化结果;预览在真正调用闲鱼之前给出可行/不可行的判断。
批次的进度与状态转换只经本模块的领域方法完成,适配层不允许直接改状态字符串。

重启续跑的语义:进程中断时处于 running 的条目不能被当成"没发生过"而自动重发——
平台侧可能已经创建了商品。这类条目恢复时记为失败并标注需人工核对,只有 pending
条目会被重新排队。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from .sku import ItemVariant, parse_price, parse_stock, spec_key, specs_from_stored, specs_to_stored

BatchState = Literal["pending", "running", "completed", "cancelled"]
EntryState = Literal["pending", "running", "succeeded", "failed", "skipped"]
PostageMode = Literal["free", "fixed"]

MAX_TITLE_LENGTH = 255
INTERRUPTED_ERROR = "执行中断,未确认平台侧是否已创建,请人工核对后再重试"


@dataclass(frozen=True)
class PublishDraft:
    """One normalized row of a batch import."""

    account_id: str
    title: str
    price: Decimal | None = None
    description: str | None = None
    category_id: str | None = None
    images: tuple[str, ...] = ()
    quantity: int = 1
    original_price: Decimal | None = None
    postage_mode: PostageMode = "free"
    postage: Decimal | None = None
    variants: tuple[ItemVariant, ...] = ()
    delivery_policy_id: int | None = None
    reply_policy_id: int | None = None
    inventory_pool_id: int | None = None
    source_row: int | None = None
    source: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            msg = "account_id 不能为空"
            raise ValueError(msg)
        if not self.title.strip():
            msg = "title 不能为空"
            raise ValueError(msg)
        if self.quantity < 1:
            msg = "quantity 至少为 1"
            raise ValueError(msg)
        for value, name in ((self.price, "price"), (self.original_price, "original_price")):
            if value is not None and value < 0:
                msg = f"{name} 不能为负数"
                raise ValueError(msg)
        if self.postage is not None and self.postage < 0:
            msg = "postage 不能为负数"
            raise ValueError(msg)

    def __repr__(self) -> str:  # pragma: no cover - 避免把整行原文带进日志
        return f"PublishDraft(row={self.source_row}, title={self.title!r})"


@dataclass(frozen=True)
class DraftProblem:
    """One reason a draft cannot be published as it stands."""

    code: str
    detail: str


@dataclass(frozen=True)
class DraftPreviewRow:
    """Preview verdict for one draft."""

    index: int
    draft: PublishDraft
    problems: tuple[DraftProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class BatchPreview:
    """Preview of a whole batch before anything is published."""

    rows: tuple[DraftPreviewRow, ...]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def ready(self) -> int:
        return sum(1 for row in self.rows if row.ok)

    @property
    def blocked(self) -> int:
        return self.total - self.ready


def preview_drafts(drafts: tuple[PublishDraft, ...]) -> BatchPreview:
    """Check every draft and flag duplicates inside the same batch.

    Only rules that can be proven locally are applied: a draft is blocked when a
    required field is missing or contradictory. Anything the platform must decide
    (title policy, category validity) is deliberately not guessed here.
    """
    seen: dict[tuple[str, str, tuple[tuple[str, str], ...]], int] = {}
    rows: list[DraftPreviewRow] = []
    for index, draft in enumerate(drafts):
        problems = list(validate_draft(draft))
        signature = _duplicate_signature(draft)
        first_index = seen.get(signature)
        if first_index is None:
            seen[signature] = index
        else:
            problems.append(
                DraftProblem(
                    code="duplicate_row",
                    detail=f"与第 {first_index + 1} 行标题、价格与规格完全相同",
                )
            )
        rows.append(DraftPreviewRow(index=index, draft=draft, problems=tuple(problems)))
    return BatchPreview(rows=tuple(rows))


def validate_draft(draft: PublishDraft) -> tuple[DraftProblem, ...]:
    """Return the locally provable problems of one draft."""
    problems: list[DraftProblem] = []
    title = draft.title.strip()
    if not title:
        problems.append(DraftProblem(code="title_required", detail="标题不能为空"))
    elif len(title) > MAX_TITLE_LENGTH:
        problems.append(
            DraftProblem(code="title_too_long", detail=f"标题超过 {MAX_TITLE_LENGTH} 字")
        )
    if draft.price is None:
        problems.append(DraftProblem(code="price_required", detail="价格不能为空"))
    elif draft.price <= 0:
        problems.append(DraftProblem(code="price_invalid", detail="价格必须大于 0"))
    if draft.postage_mode == "fixed" and draft.postage is None:
        problems.append(DraftProblem(code="postage_required", detail="固定运费必须给出金额"))
    if (
        draft.original_price is not None
        and draft.price is not None
        and draft.original_price < draft.price
    ):
        problems.append(DraftProblem(code="original_price_invalid", detail="原价不能低于售价"))
    spec_keys = [spec_key(variant.specs) for variant in draft.variants]
    if len(set(spec_keys)) != len(spec_keys):
        problems.append(DraftProblem(code="variant_duplicate", detail="同一行存在重复规格组合"))
    return tuple(problems)


def _duplicate_signature(draft: PublishDraft) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    specs = tuple(sorted(key for variant in draft.variants for key in spec_key(variant.specs)))
    price = "" if draft.price is None else str(draft.price)
    return (draft.title.strip(), price, specs)


def draft_to_stored(draft: PublishDraft) -> dict[str, object]:
    """Render a draft for its auxiliary JSON column."""
    return {
        "account_id": draft.account_id,
        "title": draft.title,
        "price": _money(draft.price),
        "description": draft.description,
        "category_id": draft.category_id,
        "images": list(draft.images),
        "quantity": draft.quantity,
        "original_price": _money(draft.original_price),
        "postage_mode": draft.postage_mode,
        "postage": _money(draft.postage),
        "variants": [_variant_to_stored(variant) for variant in draft.variants],
        "delivery_policy_id": draft.delivery_policy_id,
        "reply_policy_id": draft.reply_policy_id,
        "inventory_pool_id": draft.inventory_pool_id,
        "source_row": draft.source_row,
        "source": dict(draft.source),
    }


def draft_from_stored(raw: object) -> PublishDraft:
    """Rebuild a draft from its auxiliary JSON column."""
    if not isinstance(raw, dict):
        msg = "草稿存储格式必须是对象"
        raise TypeError(msg)
    images = raw.get("images")
    variants = raw.get("variants")
    source = raw.get("source")
    return PublishDraft(
        account_id=str(raw.get("account_id") or ""),
        title=str(raw.get("title") or ""),
        price=parse_price(raw.get("price")),
        description=_text_or_none(raw.get("description")),
        category_id=_text_or_none(raw.get("category_id")),
        images=tuple(str(url) for url in images) if isinstance(images, list) else (),
        quantity=parse_stock(raw.get("quantity")) or 1,
        original_price=parse_price(raw.get("original_price")),
        postage_mode="fixed" if raw.get("postage_mode") == "fixed" else "free",
        postage=parse_price(raw.get("postage")),
        variants=(
            tuple(_variant_from_stored(entry) for entry in variants)
            if isinstance(variants, list)
            else ()
        ),
        delivery_policy_id=_int_or_none(raw.get("delivery_policy_id")),
        reply_policy_id=_int_or_none(raw.get("reply_policy_id")),
        inventory_pool_id=_int_or_none(raw.get("inventory_pool_id")),
        source_row=_int_or_none(raw.get("source_row")),
        source={str(key): str(value) for key, value in source.items()}
        if isinstance(source, dict)
        else {},
    )


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _variant_to_stored(variant: ItemVariant) -> dict[str, object]:
    return {
        "external_sku_id": variant.external_sku_id,
        "specs": specs_to_stored(variant.specs),
        "price": _money(variant.price),
        "stock": variant.stock,
        "image_url": variant.image_url,
        "enabled": variant.enabled,
    }


def _variant_from_stored(raw: object) -> ItemVariant:
    if not isinstance(raw, dict):
        msg = "规格存储格式必须是对象"
        raise TypeError(msg)
    return ItemVariant(
        specs=specs_from_stored(raw.get("specs")),
        external_sku_id=_text_or_none(raw.get("external_sku_id")),
        price=parse_price(raw.get("price")),
        stock=parse_stock(raw.get("stock")) or 0,
        image_url=_text_or_none(raw.get("image_url")),
        enabled=bool(raw.get("enabled", True)),
    )


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        # value 是任意对象:能转成 int 就转,转不了由下面捕获;这里只需屏蔽重载类型告警。
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class BatchPublishEntry:
    """One draft inside a persisted batch."""

    index: int
    draft: PublishDraft
    state: EntryState = "pending"
    external_item_id: str | None = None
    error: str | None = None
    attempts: int = 0


@dataclass(frozen=True)
class BatchProgress:
    """Derived counters; the entries are the single source of truth."""

    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    skipped: int

    @property
    def finished(self) -> bool:
        return self.pending == 0 and self.running == 0


@dataclass(frozen=True)
class BatchPublishJob:
    """A persisted batch of publish drafts."""

    id: str
    account_id: str
    entries: tuple[BatchPublishEntry, ...]
    state: BatchState = "pending"
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            msg = "批次 id 不能为空"
            raise ValueError(msg)
        if not self.account_id.strip():
            msg = "account_id 不能为空"
            raise ValueError(msg)
        if not self.entries:
            msg = "批次必须至少包含一行草稿"
            raise ValueError(msg)
        indexes = [entry.index for entry in self.entries]
        if len(set(indexes)) != len(indexes):
            msg = "批次的条目序号不能重复"
            raise ValueError(msg)

    @property
    def progress(self) -> BatchProgress:
        counts: dict[EntryState, int] = {
            "pending": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }
        for entry in self.entries:
            counts[entry.state] += 1
        return BatchProgress(
            total=len(self.entries),
            pending=counts["pending"],
            running=counts["running"],
            succeeded=counts["succeeded"],
            failed=counts["failed"],
            skipped=counts["skipped"],
        )

    def start(self) -> BatchPublishJob:
        """Mark the batch as running; other states are left untouched."""
        if self.state != "pending":
            return self
        return replace(self, state="running", started_at=datetime.now(UTC))

    def next_pending(self) -> BatchPublishEntry | None:
        """Return the next entry to execute, in row order."""
        if self.state == "cancelled":
            return None
        return next((entry for entry in self.entries if entry.state == "pending"), None)

    def with_entry(self, entry: BatchPublishEntry) -> BatchPublishJob:
        """Replace one entry by index and settle the batch state."""
        updated = tuple(
            entry if current.index == entry.index else current for current in self.entries
        )
        job = replace(self, entries=updated)
        if self.state in {"cancelled"}:
            return job
        if job.progress.finished:
            return replace(job, state="completed", finished_at=datetime.now(UTC))
        return replace(job, state="running")

    def recover_after_restart(self) -> BatchPublishJob:
        """Re-queue pending rows and flag interrupted ones for human review.

        An entry recorded as running was interrupted mid-flight: the platform may
        already hold a created item, so it is failed with an explicit reason
        instead of being silently published a second time.
        """
        recovered: list[BatchPublishEntry] = []
        changed = False
        for entry in self.entries:
            if entry.state != "running":
                recovered.append(entry)
                continue
            changed = True
            recovered.append(
                replace(entry, state="failed", error=INTERRUPTED_ERROR, attempts=entry.attempts)
            )
        job = replace(self, entries=tuple(recovered))
        if not changed and self.state != "pending":
            return job
        return replace(job, state="pending", finished_at=None)
