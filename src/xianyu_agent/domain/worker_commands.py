"""Compatibility alias for canonical runtime command domain module."""

import sys

from xianyu_agent.domain.runtime import command as _canonical

sys.modules[__name__] = _canonical
