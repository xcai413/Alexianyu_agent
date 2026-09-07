"""Lease value objects shared by scheduler adapters and runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .clock import require_utc


@dataclass(frozen=True, slots=True)
class LeasedWork:
    """Opaque due work claimed by one scheduler worker."""

    work_id: str
    lease_token: str
    lease_owner: str
    leased_until: datetime
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.work_id:
            raise ValueError("work_id must not be empty")
        if not self.lease_token:
            raise ValueError("lease_token must not be empty")
        if not self.lease_owner:
            raise ValueError("lease_owner must not be empty")
        require_utc(self.leased_until)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def expired_at(self, now: datetime) -> bool:
        return self.leased_until <= require_utc(now)
