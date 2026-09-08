"""Infrastructure adapters for application credential/session contracts."""

from .ws_credentials import LegacyValidationGate, LegacyWsCredentialBackend

__all__ = ["LegacyValidationGate", "LegacyWsCredentialBackend"]
