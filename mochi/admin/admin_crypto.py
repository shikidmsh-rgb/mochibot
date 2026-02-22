"""API key encryption at rest using Fernet (optional dependency).

Derives the encryption key from ADMIN_TOKEN via PBKDF2, so no separate
key file is needed. Falls back to plaintext storage with a warning if
the `cryptography` package is not installed.
"""

import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

_PBKDF2_SALT = b"mochibot-admin-key-v1"  # static salt (key derived from ADMIN_TOKEN)
_PBKDF2_ITERATIONS = 480_000

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    InvalidToken = Exception  # type: ignore[misc,assignment]

_fernet_instance: "Fernet | None" = None
_warned = False


def _get_fernet() -> "Fernet | None":
    """Return a cached Fernet instance derived from ADMIN_TOKEN, or None."""
    global _fernet_instance, _warned

    if _fernet_instance is not None:
        return _fernet_instance

    if not _HAS_CRYPTO:
        if not _warned:
            log.warning(
                "cryptography package not installed — API keys stored in plaintext. "
                "Install with: pip install cryptography"
            )
            _warned = True
        return None

    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        if not _warned:
            log.warning("ADMIN_TOKEN not set — cannot derive encryption key")
            _warned = True
        return None

    # Derive a 32-byte key via PBKDF2-HMAC-SHA256, then base64 for Fernet
    dk = hashlib.pbkdf2_hmac(
        "sha256", token.encode(), _PBKDF2_SALT, _PBKDF2_ITERATIONS
    )
    key = base64.urlsafe_b64encode(dk)
    _fernet_instance = Fernet(key)
    return _fernet_instance


def is_encrypted(value: str) -> bool:
    """Check if a value looks like a Fernet token."""
    return bool(value) and value.startswith("gAAAAA")


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key for storage. Returns ciphertext or plaintext as fallback."""
    if not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        return plaintext  # already encrypted, avoid double encryption

    f = _get_fernet()
    if f is None:
        return plaintext  # fallback: store as-is

    return f.encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an API key from storage. Handles plaintext inputs gracefully."""
    if not ciphertext:
        return ciphertext
    if not is_encrypted(ciphertext):
        return ciphertext  # plaintext (legacy or fallback), return as-is

    f = _get_fernet()
    if f is None:
        log.warning("Cannot decrypt key — cryptography not available or ADMIN_TOKEN missing")
        return ""

    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        log.warning("Failed to decrypt API key (token may have changed)")
        return ""


def reset_cache() -> None:
    """Clear the cached Fernet instance (e.g. after ADMIN_TOKEN change)."""
    global _fernet_instance, _warned
    _fernet_instance = None
    _warned = False
