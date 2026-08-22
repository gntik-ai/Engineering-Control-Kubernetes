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
