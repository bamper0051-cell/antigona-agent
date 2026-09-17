"""Пакет безопасности Antigona.

Содержит модули для:
- Проверки личности владельца (OwnerIdentity)
- Повышенных прав владельца (OwnerOverrideManager)
- Классификации рисков действий (RiskClassifier, RiskLevel)
- Чтения приватных credentials (verifier_credential, read_private_credential)
- Одноразовых кодов через Telegram (OTPManager, OTPChallenge)
- TOTP-аутентификации через authenticator-приложения (TOTPManager)
- Безопасного аудиторского журнала (SystemAuditLogger, sanitize_secrets)
"""

from antigona.security._credentials import (
    read_private_credential,
    verifier_credential,
)
from antigona.security.audit import SystemAuditLogger, sanitize_secrets
from antigona.security.auth_service import AuthService, cli_principal
from antigona.security.otp import OTPChallenge, OTPManager
from antigona.security.owner_identity import OwnerIdentity
from antigona.security.owner_override import OwnerOverrideManager
from antigona.security.risk_classifier import RiskClassifier, RiskLevel
from antigona.security.totp import TOTPManager

__all__ = [
    "AuthService",
    "cli_principal",
    "OwnerIdentity",
    "OwnerOverrideManager",
    "RiskClassifier",
    "RiskLevel",
    "OTPManager",
    "OTPChallenge",
    "TOTPManager",
    "SystemAuditLogger",
    "sanitize_secrets",
    "read_private_credential",
    "verifier_credential",
]
