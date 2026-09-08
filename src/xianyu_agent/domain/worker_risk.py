"""Compatibility alias for canonical account risk domain module."""

import sys

from xianyu_agent.domain.account import risk as _canonical

sys.modules[__name__] = _canonical
