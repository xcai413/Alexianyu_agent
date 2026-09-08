"""Compatibility alias for canonical order domain module."""

import sys

from xianyu_agent.domain.order import orders as _canonical

sys.modules[__name__] = _canonical
