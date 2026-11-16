"""What a client in a sandbox shows the gateway in place of an API key.

A grant names the account whose key may be used and the step it was issued for, and is
valid until a time. It is signed with a key of the platform's that no sandbox has. The
gateway accepts it only while that step is running, so it ends with the step whatever
its time says.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final
from uuid import UUID

PREFIX: Final = "ecg1"
# Longer than any step may last: the step's end is what ends a grant.
SECONDS: Final = 4 * 3600


class GrantError(ValueError):
    """The grant is not one the platform issued, or its time is over."""


def _signature(key: bytes, body: str) -> str:
    return hmac.new(key, f"gateway:{body}".encode(), hashlib.sha256).hexdigest()


def issue(key: bytes, *, account_id: UUID, step_id: UUID, seconds: int = SECONDS) -> str:
    """A grant for one step to use one account's key through the gateway."""
    body = f"{account_id.hex}.{step_id.hex}.{int(time.time()) + seconds}"
    return f"{PREFIX}.{body}.{_signature(key, body)}"


def read(key: bytes, grant: str, *, now: float | None = None) -> tuple[UUID, UUID]:
    """The account and the step a grant is for. Raises ``GrantError`` for anything that
    is not a grant of this platform's that is still valid."""
    parts = (grant or "").strip().split(".")
    if len(parts) != 5 or parts[0] != PREFIX:
        raise GrantError("That is not a grant of this platform")
    body = ".".join(parts[1:4])
    if not hmac.compare_digest(_signature(key, body), parts[4]):
        raise GrantError("The grant was not issued by this platform")
    try:
        account_id, step_id, until = UUID(hex=parts[1]), UUID(hex=parts[2]), int(parts[3])
    except ValueError as error:
        raise GrantError("The grant cannot be read") from error
    if until < (time.time() if now is None else now):
        raise GrantError("The grant's time is over")
    return account_id, step_id
