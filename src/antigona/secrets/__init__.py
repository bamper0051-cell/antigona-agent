"""Secrets package — Secret Vault for API keys and credentials.

The vault stores secrets in ``~/.hermes/vault/`` with restricted permissions.
It provides a simple key-value interface with export-to-env support.
"""

from antigona.secrets.vault import Vault

__all__ = ["Vault"]
