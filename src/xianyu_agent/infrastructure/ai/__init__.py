"""AI provider infrastructure: concrete providers and their vault-backed wiring."""

from .vault_provider import VaultBackedProvider

__all__ = ["VaultBackedProvider"]
