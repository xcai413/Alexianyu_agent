"""Compatibility alias for canonical message domain module."""

import sys

from xianyu_agent.domain.message import messages as _canonical

sys.modules[__name__] = _canonical
