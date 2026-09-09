"""Compatibility facade for the canonical read-only MTOP orders endpoint.

New protocol code should import from
:mod:`xianyu_agent.protocol.mtop.endpoints.orders`.
"""

from xianyu_agent.protocol.mtop.endpoints.orders import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SOLD_ORDERS_API,
    SOLD_ORDERS_URL,
    OrderSyncError,
    RemoteSoldOrder,
    SoldOrderPage,
    SoldOrderSnapshot,
    XianyuOrdersClient,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "SOLD_ORDERS_API",
    "SOLD_ORDERS_URL",
    "OrderSyncError",
    "RemoteSoldOrder",
    "SoldOrderPage",
    "SoldOrderSnapshot",
    "XianyuOrdersClient",
]
