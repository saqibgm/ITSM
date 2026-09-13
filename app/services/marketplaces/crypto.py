"""Encrypt/decrypt MarketplaceConnection.credentials at rest.

No shared secret-encryption utility exists elsewhere in this codebase to reuse
(checked before writing this) — derives a Fernet key from Settings.SECRET_KEY
via SHA-256 rather than requiring a brand-new required env var, matching this
repo's general preference for not growing the required-config surface unless
truly needed. If dedicated key rotation is ever wanted, swap this for a real
MARKETPLACE_CREDENTIALS_ENCRYPTION_KEY setting — the call sites (connectors)
don't need to change, only this module.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # Value predates this encryption scheme, or SECRET_KEY rotated without
        # a re-encryption pass — surface as a clear error rather than a cryptic
        # one; the connection needs reconnecting either way.
        raise ValueError("could not decrypt stored credential — reconnect this marketplace")


__all__ = ["encrypt_secret", "decrypt_secret"]
