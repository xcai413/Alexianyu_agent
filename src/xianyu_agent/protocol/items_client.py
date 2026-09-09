"""Compatibility facade for the canonical read-only MTOP items endpoint.

New protocol code should import from
:mod:`xianyu_agent.protocol.mtop.endpoints.items`.
"""

from xianyu_agent.protocol.mtop.endpoints.items import (
    DEFAULT_PAGE_SIZE,
    ITEM_LIST_API,
    ITEM_LIST_URL,
    MAX_PAGE_SIZE,
    ItemPage,
    ItemSnapshot,
    ItemSyncError,
    RemoteItem,
    XianyuItemsClient,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "ITEM_LIST_API",
    "ITEM_LIST_URL",
    "MAX_PAGE_SIZE",
    "ItemPage",
    "ItemSnapshot",
    "ItemSyncError",
    "RemoteItem",
    "XianyuItemsClient",
]
