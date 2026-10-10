"""商品类目(发布必填路径)。

平台要求类目 id、类目名与频道类目 id 同时具备:只给 id 的"半截类目"会被拒绝,而且
错误发生在发布那一刻。因此在值对象里就校验完整性,不让不完整的类目进到请求里。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublishCategory:
    """One complete publish category path."""

    cat_id: str
    cat_name: str
    channel_cat_id: str
    tb_cat_id: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.cat_id, "cat_id"),
            (self.cat_name, "cat_name"),
            (self.channel_cat_id, "channel_cat_id"),
        ):
            if not str(value).strip():
                msg = f"类目缺少 {name}"
                raise ValueError(msg)
        for field_name in ("cat_id", "cat_name", "channel_cat_id", "tb_cat_id"):
            object.__setattr__(self, field_name, str(getattr(self, field_name)).strip())

    def describe(self) -> str:
        return f"{self.cat_name}({self.cat_id}/频道 {self.channel_cat_id})"
