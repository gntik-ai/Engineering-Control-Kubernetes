"""How an API key is kept: sealed in the database, opened only where a model is asked.

The key that seals them is one of the platform's own generated secrets, mounted into the
console (which seals a key the administrator enters) and the launcher (which opens it to
ask a model). It is in no sandbox, and a copy of the database alone opens nothing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12


class SealError(Exception):
    pass


def read_sealing_key(path: Path | None) -> bytes | None:
    """The platform's key for API keys, or None when the installation has none."""
    if path is None:
        return None
    try:
        key = path.read_bytes().strip()
    except OSError:
        return None
    return key if len(key) >= 32 else None


def _cipher(key: bytes) -> AESGCM:
    return AESGCM(hashlib.sha256(b"engineering-control model keys\0" + key).digest())


def seal(key: bytes, account_id: UUID, secret: str) -> str:
    """The secret, readable only with the platform's key and only as that account's."""
    nonce = os.urandom(_NONCE)
    sealed = _cipher(key).encrypt(nonce, secret.encode(), str(account_id).encode())
    return base64.b64encode(nonce + sealed).decode("ascii")


def unseal(key: bytes, account_id: UUID, sealed: str) -> str:
    try:
        raw = base64.b64decode(sealed.encode("ascii"), validate=True)
        opened = _cipher(key).decrypt(raw[:_NONCE], raw[_NONCE:], str(account_id).encode())
        return opened.decode()
    except (binascii.Error, InvalidTag, ValueError, UnicodeDecodeError) as error:
        raise SealError(
            "The account's key cannot be opened with this installation's key. Enter the "
            "API key again."
        ) from error
