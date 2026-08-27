from __future__ import annotations

from dataclasses import dataclass

from engineering_control.dashboard.auth import SessionData


@dataclass(frozen=True)
class AuthenticatedSession:
    token: str
    data: SessionData
