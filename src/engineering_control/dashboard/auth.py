from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

PASSWORD_MIN_LENGTH = 14
PASSWORD_MAX_LENGTH = 128
SESSION_COOKIE = "engineering_dashboard_session"
LOGIN_CSRF_COOKIE = "engineering_dashboard_login_csrf"
SESSION_CLOCK_SKEW_SECONDS = 60


class PasswordService:
    def __init__(self) -> None:
        self.hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=2,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(24))

    def generate(self) -> str:
        return secrets.token_urlsafe(32)

    def hash(self, password: str) -> str:
        self.validate(password)
        return self.hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        candidate = password_hash or self._dummy_hash
        try:
            valid: bool = self.hasher.verify(candidate, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            valid = False
        return valid and password_hash is not None

    @staticmethod
    def validate(password: str) -> None:
        if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
            raise ValueError("password must contain 14 to 128 characters")
        if "\x00" in password or "\r" in password or "\n" in password:
            raise ValueError("password contains unsupported control characters")


@dataclass(frozen=True)
class SessionData:
    password_version: int
    issued_at: int
    expires_at: int
    csrf_token: str


class SignedSessionManager:
    def __init__(self, key: bytes, *, lifetime_seconds: int = 43_200) -> None:
        if len(key) < 32:
            raise ValueError("dashboard session key is too short")
        self.key = key
        self.lifetime_seconds = lifetime_seconds

    def create(self, password_version: int, *, now: int | None = None) -> str:
        issued = int(time.time()) if now is None else now
        payload = {
            "v": password_version,
            "iat": issued,
            "exp": issued + self.lifetime_seconds,
            "csrf": secrets.token_urlsafe(24),
        }
        return self._sign(payload)

    def verify(self, token: str, *, now: int | None = None) -> SessionData | None:
        payload = self._verify_payload(token)
        if payload is None:
            return None
        current = int(time.time()) if now is None else now
        try:
            version = int(payload["v"])
            issued = int(payload["iat"])
            expires = int(payload["exp"])
            csrf = str(payload["csrf"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            version < 1
            or issued > current + SESSION_CLOCK_SKEW_SECONDS
            or expires <= current
            or expires <= issued
        ):
            return None
        if expires - issued > self.lifetime_seconds or len(csrf) < 20:
            return None
        return SessionData(version, issued, expires, csrf)

    def create_login_csrf(self, *, now: int | None = None) -> str:
        issued = int(time.time()) if now is None else now
        return self._sign({"purpose": "login", "iat": issued, "nonce": secrets.token_urlsafe(24)})

    def verify_login_csrf(self, token: str, *, now: int | None = None) -> bool:
        payload = self._verify_payload(token)
        if payload is None or payload.get("purpose") != "login":
            return False
        current = int(time.time()) if now is None else now
        try:
            issued = int(payload["iat"])
            nonce = str(payload["nonce"])
        except (KeyError, TypeError, ValueError):
            return False
        return current - 900 <= issued <= current + SESSION_CLOCK_SKEW_SECONDS and len(nonce) >= 20

    def binding_digest(self, token: str) -> str:
        """Return a non-reversible session binding without persisting cookie material."""
        return hmac.new(
            self.key, b"dashboard-action-binding\0" + token.encode(), hashlib.sha256
        ).hexdigest()

    def _sign(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
        signature = hmac.new(self.key, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def _verify_payload(self, token: str) -> dict[str, Any] | None:
        try:
            encoded, signature = token.split(".", 1)
            supplied = _b64decode(signature)
            expected = hmac.new(self.key, encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                return None
            value = json.loads(_b64decode(encoded))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
            return None
        return value if isinstance(value, dict) else None


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class LoginFailureLimiter:
    def __init__(self, *, maximum_failures: int = 5, window_seconds: int = 900) -> None:
        self.maximum_failures = maximum_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, address: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._expire(address, current)
            return len(self._failures[address]) >= self.maximum_failures

    def failure(self, address: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._expire(address, current)
            self._failures[address].append(current)

    def success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)

    def _expire(self, address: str, now: float) -> None:
        values = self._failures[address]
        cutoff = now - self.window_seconds
        while values and values[0] < cutoff:
            values.popleft()
        if not values:
            self._failures.pop(address, None)


def read_session_key(path: Path, *, expected_group_id: int | None = None) -> bytes:
    parent = path.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o750
        or parent_metadata.st_uid != 0
        or (expected_group_id is not None and parent_metadata.st_gid != expected_group_id)
    ):
        raise PermissionError("dashboard session-key directory metadata is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o640:
            raise PermissionError("dashboard session-key metadata is invalid")
        if metadata.st_uid != 0:
            raise PermissionError("dashboard session key must be root-owned")
        if expected_group_id is not None and metadata.st_gid != expected_group_id:
            raise PermissionError("dashboard session key has the wrong service group")
        key = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    if len(key) < 32 or len(key) > 1024:
        raise PermissionError("dashboard session key has an invalid size")
    return key


@dataclass
class StagedPasswordOutput:
    path: Path
    temporary: Path
    owner_uid: int
    owner_gid: int

    def commit(self) -> Path:
        os.chown(self.temporary, self.owner_uid, self.owner_gid)
        os.chmod(self.temporary, 0o600)
        os.replace(self.temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return self.path

    def discard(self) -> None:
        self.temporary.unlink(missing_ok=True)


def stage_password_output(
    path: Path, password: str, *, allow_replace: bool = False
) -> StagedPasswordOutput:
    if not path.is_absolute():
        raise ValueError("password output path must be absolute")
    parent = path.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise PermissionError("password output parent must be a real directory")
    if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise PermissionError("password output parent permissions are too broad")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if not allow_replace:
            raise FileExistsError("password output file already exists")
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("existing password output file metadata is unsafe")
    temporary = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(password)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        return StagedPasswordOutput(
            path=path,
            temporary=temporary,
            owner_uid=parent_metadata.st_uid,
            owner_gid=parent_metadata.st_gid,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_password_atomically(path: Path, password: str, *, allow_replace: bool = False) -> Path:
    staged = stage_password_output(path, password, allow_replace=allow_replace)
    try:
        return staged.commit()
    finally:
        staged.discard()
