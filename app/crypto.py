"""Token encryption utilities for securing account secrets at rest.

Uses versioned Fernet keys derived from configured base secrets so deployments
can rotate encryption keys without breaking decryption of existing data.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken

from app.config import settings

_KEY_PREFIX = "v2"
_LEGACY_PREFIX = "v1"
_KDF_CONTEXT = b"curatio:token-encryption:v2"


def _derive_fernet_key(secret: str, *, context: bytes = _KDF_CONTEXT) -> bytes:
    """Derive a deterministic Fernet key from a base secret."""
    digest = hmac.new(secret.encode("utf-8"), context, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)


def _legacy_fernet() -> Fernet:
    """Legacy key derivation retained for backwards-compatible decryption."""
    key = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _configured_fernets() -> list[Fernet]:
    """Return active + fallback Fernet instances from configured key material."""
    raw_keys = [
        k.strip() for k in settings.security_encryption_keys.split(",") if k.strip()
    ]
    if not raw_keys:
        raw_keys = [settings.secret_key]
    return [Fernet(_derive_fernet_key(secret)) for secret in raw_keys]


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token for storage using the active key."""
    ciphertext = _configured_fernets()[0].encrypt(plaintext.encode()).decode()
    return f"{_KEY_PREFIX}:{ciphertext}"


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored token, supporting key rotation and legacy tokens."""
    if ":" in ciphertext:
        prefix, token = ciphertext.split(":", 1)
        if prefix == _KEY_PREFIX:
            for fernet in _configured_fernets():
                try:
                    return fernet.decrypt(token.encode()).decode()
                except InvalidToken:
                    continue
        if prefix == _LEGACY_PREFIX:
            return _legacy_fernet().decrypt(token.encode()).decode()
    for fernet in _configured_fernets():
        try:
            return fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            continue
    return _legacy_fernet().decrypt(ciphertext.encode()).decode()
