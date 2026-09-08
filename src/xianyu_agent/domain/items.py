"""Compatibility alias for canonical item domain module."""

import sys

from xianyu_agent.domain.item import items as _canonical

sys.modules[__name__] = _canonical
