"""Compatibility alias for canonical soak-monitor application service."""

import sys

from xianyu_agent.application.health import soak as _canonical

sys.modules[__name__] = _canonical
