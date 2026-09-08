"""Compatibility alias for canonical inventory cards domain module."""

import sys

from xianyu_agent.domain.inventory import cards as _canonical

sys.modules[__name__] = _canonical
