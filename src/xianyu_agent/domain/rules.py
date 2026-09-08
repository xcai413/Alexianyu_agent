"""Compatibility alias for canonical message reply-rule domain module."""

import sys

from xianyu_agent.domain.message import rules as _canonical

sys.modules[__name__] = _canonical
