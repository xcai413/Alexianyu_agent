"""Compatibility alias for canonical runtime daemon domain module."""

import sys

from xianyu_agent.domain.runtime import daemon as _canonical

sys.modules[__name__] = _canonical
