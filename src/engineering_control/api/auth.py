from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, status


def bearer_auth(expected_token: str) -> Callable[..., Awaitable[str]]:
    expected_digest = hashlib.sha256(expected_token.encode()).digest()

    async def authenticate(
        authorization: str | None = Header(default=None),
        x_engineering_actor: str | None = Header(default=None),
    ) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authenticated Engineering Control access required",
            )
        supplied = authorization.removeprefix("Bearer ").strip()
        supplied_digest = hashlib.sha256(supplied.encode()).digest()
        if not hmac.compare_digest(expected_digest, supplied_digest):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
        actor = (x_engineering_actor or "api-client").strip()
        if not re.fullmatch(r"[A-Za-z0-9:@._-]{1,128}", actor):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid actor")
        return actor

    return authenticate


def identity_bearer_auth(tokens: dict[str, str]) -> Callable[..., Awaitable[str]]:
    if not tokens:
        raise ValueError("at least one identity token is required")
    expected = {
        identity: hashlib.sha256(token.encode()).digest() for identity, token in tokens.items()
    }

    async def authenticate(
        authorization: str | None = Header(default=None),
        x_engineering_actor: str | None = Header(default=None),
    ) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authenticated evidence upload required",
            )
        actor = (x_engineering_actor or "").strip()
        if actor not in expected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
        supplied = authorization.removeprefix("Bearer ").strip()
        supplied_digest = hashlib.sha256(supplied.encode()).digest()
        if not hmac.compare_digest(expected[actor], supplied_digest):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
        return actor

    return authenticate
