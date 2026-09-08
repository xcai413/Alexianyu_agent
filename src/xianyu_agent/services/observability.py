"""Compatibility alias for canonical runtime observability application service."""

import sys

from xianyu_agent.application.health import observability as _canonical

sys.modules[__name__] = _canonical
