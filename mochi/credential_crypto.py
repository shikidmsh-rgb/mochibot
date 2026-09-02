"""Encrypt stored credentials with a key derived from ADMIN_TOKEN."""

import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

_PBKDF2_SALT = b"mochibot-admin-key-v1"
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
    global _fernet_instance, _warned

    if _fernet_instance is not None:
        return _fernet_instance
    if not _HAS_CRYPTO:
        if not _warned:
            log.warning("cryptography is unavailable; credentials cannot be encrypted")
            _warned = True
        return None

    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        if not _warned:
            log.warning("ADMIN_TOKEN is not set; credentials cannot be encrypted")
            _warned = True
        return None

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        token.encode(),
        _PBKDF2_SALT,
        _PBKDF2_ITERATIONS,
    )
    _fernet_instance = Fernet(base64.urlsafe_b64encode(derived))
    return _fernet_instance


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith("gAAAAA")


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    fernet = _get_fernet()
    if fernet is None:
        return plaintext
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext or not is_encrypted(ciphertext):
        return ciphertext
    fernet = _get_fernet()
    if fernet is None:
        log.warning("Stored credential cannot be decrypted")
        return ""
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        log.warning("Stored credential cannot be decrypted with the current ADMIN_TOKEN")
        return ""
