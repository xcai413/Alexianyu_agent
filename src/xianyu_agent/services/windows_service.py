"""Compatibility alias for the canonical Windows runtime platform module."""

import sys

from xianyu_agent.runtime.platform import windows_service as _canonical

sys.modules[__name__] = _canonical
