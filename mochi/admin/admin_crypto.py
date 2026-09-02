"""Compatibility aliases for credential encryption."""

from mochi.credential_crypto import (
    decrypt_secret as decrypt_api_key,
    encrypt_secret as encrypt_api_key,
    is_encrypted,
)

__all__ = ["decrypt_api_key", "encrypt_api_key", "is_encrypted"]
