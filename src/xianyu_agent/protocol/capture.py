"""Compatibility facade for canonical protocol diagnostics capture helpers.

New code should import from :mod:`xianyu_agent.protocol.diagnostics.capture`.
"""

from xianyu_agent.protocol.diagnostics.capture import (
    CalibrationRecorder,
    CaptureCounts,
    CaptureVerification,
    capture_output_conflict,
    redact_structure,
    verify_capture,
)

__all__ = [
    "CalibrationRecorder",
    "CaptureCounts",
    "CaptureVerification",
    "capture_output_conflict",
    "redact_structure",
    "verify_capture",
]
