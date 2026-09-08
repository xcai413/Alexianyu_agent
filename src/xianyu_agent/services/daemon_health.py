"""Compatibility alias for canonical runtime-health application service."""

import sys

from xianyu_agent.application.health import runtime_health as _canonical

sys.modules[__name__] = _canonical
