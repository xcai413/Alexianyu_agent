"""Compatibility alias for canonical account state domain module."""

import sys

from xianyu_agent.domain.account import state as _canonical

sys.modules[__name__] = _canonical
