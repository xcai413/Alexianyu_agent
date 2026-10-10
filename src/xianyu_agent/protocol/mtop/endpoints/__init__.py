"""Canonical read-only MTOP endpoint clients."""

from .adjust_price import (
    AdjustPriceError,
    AdjustPriceRejectedError,
    AdjustPriceResult,
    AdjustPriceUncertainError,
    XianyuAdjustPriceClient,
)
from .consign import (
    ConsignError,
    ConsignRejectedError,
    ConsignResult,
    ConsignUncertainError,
    XianyuConsignClient,
)
from .item_polish import ItemPolishError, PolishResult, XianyuItemPolishClient
from .items import ItemPage, ItemSnapshot, ItemSyncError, RemoteItem, XianyuItemsClient
from .order_read import (
    OrderReadError,
    XianyuOrderReadClient,
    parse_consign_info,
    parse_logistics_info,
    parse_refund_info,
)
from .orders import (
    OrderSyncError,
    RemoteSoldOrder,
    SoldOrderPage,
    SoldOrderSnapshot,
    XianyuOrdersClient,
)
from .user_profile import (
    UserProfile,
    UserProfileError,
    XianyuUserProfileClient,
)

__all__ = [
    "AdjustPriceError",
    "AdjustPriceRejectedError",
    "AdjustPriceResult",
    "AdjustPriceUncertainError",
    "ConsignError",
    "ConsignRejectedError",
    "ConsignResult",
    "ConsignUncertainError",
    "ItemPage",
    "ItemPolishError",
    "ItemSnapshot",
    "ItemSyncError",
    "OrderReadError",
    "OrderSyncError",
    "PolishResult",
    "RemoteItem",
    "RemoteSoldOrder",
    "SoldOrderPage",
    "SoldOrderSnapshot",
    "UserProfile",
    "UserProfileError",
    "XianyuAdjustPriceClient",
    "XianyuConsignClient",
    "XianyuItemPolishClient",
    "XianyuItemsClient",
    "XianyuOrderReadClient",
    "XianyuOrdersClient",
    "XianyuUserProfileClient",
    "parse_consign_info",
    "parse_logistics_info",
    "parse_refund_info",
]
