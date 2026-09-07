"""Stable pagination, sorting and query semantics for shared adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class SortSpec:
    field: str
    direction: SortDirection = SortDirection.ASC

    def __post_init__(self) -> None:
        field = self.field.strip()
        if not field:
            raise ValueError("sort field must not be blank")
        object.__setattr__(self, "field", field)


@dataclass(frozen=True, slots=True)
class PageRequest:
    """One-based page request with bounded page size and explicit ordering."""

    page: int = 1
    page_size: int = 50
    sort: tuple[SortSpec, ...] = ()
    query: str | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be >= 1")
        if not 1 <= self.page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if self.query is not None:
            query = self.query.strip()
            object.__setattr__(self, "query", query or None)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass(frozen=True, slots=True)
class PageResult(Generic[T]):
    items: tuple[T, ...]
    total: int
    page: int
    page_size: int

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total must be >= 0")
        if self.page < 1:
            raise ValueError("page must be >= 1")
        if self.page_size < 1:
            raise ValueError("page_size must be >= 1")

    @property
    def pages(self) -> int:
        if self.total == 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size
