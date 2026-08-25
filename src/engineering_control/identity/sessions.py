from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from temporalio.exceptions import ApplicationError

from engineering_control.identity.credentials import SuperadminCredential
from engineering_control.security.redaction import SecretRedactor


@dataclass(repr=False)
class EphemeralSuperadminSession:
    project: str
    base_url: str
    client: httpx.AsyncClient = field(repr=False)
    session_id: str | None = field(default=None, repr=False)
    _refresh_token: str | None = field(default=None, repr=False)
    _secrets: list[str] = field(default_factory=list, repr=False)
    superadmin_role_valid: bool = False

    def safe_summary(self) -> dict[str, object]:
        return {
            "project": self.project,
            "authentication_valid": True,
            "superadmin_role_valid": self.superadmin_role_valid,
        }


class SuperadminSessionAdapter:
    """Authenticate and destroy protocol material inside one browser-worker activity."""

    def __init__(
        self,
        *,
        redactor: SecretRedactor,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.redactor = redactor
        self.transport = transport

    @asynccontextmanager
    async def login(
        self, credential: SuperadminCredential
    ) -> AsyncIterator[EphemeralSuperadminSession]:
        self.redactor.register_secret(credential.username)
        self.redactor.register_secret(credential.password)
        client = httpx.AsyncClient(
            base_url=credential.base_url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            verify=True,
            transport=self.transport,
        )
        session = EphemeralSuperadminSession(
            project=credential.project,
            base_url=credential.base_url,
            client=client,
        )
        cleanup_failed = False
        try:
            response = await client.post(
                self._login_path(credential.project),
                json=self._login_body(credential),
                headers={"Accept": "application/json", "Cache-Control": "no-store"},
            )
            if response.status_code not in {200, 201}:
                raise ApplicationError(
                    "superadmin authentication failed",
                    type="SUPERADMIN_AUTHENTICATION_FAILED",
                    non_retryable=True,
                )
            value = self._object(response)
            tokens = self._session_material(credential.project, value)
            cookies = [item for item in client.cookies.values() if item]
            material = [*tokens, *cookies]
            if not material:
                raise ApplicationError(
                    "superadmin login returned no bounded session material",
                    type="SUPERADMIN_AUTHENTICATION_FAILED",
                    non_retryable=True,
                )
            for secret in material:
                self.redactor.register_secret(secret)
                session._secrets.append(secret)
            if tokens:
                client.headers["Authorization"] = f"Bearer {tokens[0]}"
            session.session_id = self._session_id(value)
            session._refresh_token = self._refresh_token(value)
            if credential.project == "falcone" and (
                session.session_id is None or session._refresh_token is None
            ):
                cleanup_failed = True
                raise ApplicationError(
                    "Falcone login returned no revocable session",
                    type="SUPERADMIN_AUTHENTICATION_FAILED",
                    non_retryable=True,
                )
            session.superadmin_role_valid = self._superadmin_role(value)
            if not session.superadmin_role_valid:
                raise ApplicationError(
                    "authenticated identity is not a superadmin",
                    type="SUPERADMIN_ROLE_INVALID",
                    non_retryable=True,
                )
            yield session
        finally:
            try:
                try:
                    await self._logout(session)
                except httpx.HTTPError:
                    cleanup_failed = True
            finally:
                try:
                    client.cookies.clear()
                    client.headers.pop("Authorization", None)
                    try:
                        await client.aclose()
                    except httpx.HTTPError:
                        cleanup_failed = True
                finally:
                    for secret in session._secrets:
                        self.redactor.unregister_secret(secret)
                    session._secrets.clear()
                    session.session_id = None
                    session._refresh_token = None
                    self.redactor.unregister_secret(credential.username)
                    self.redactor.unregister_secret(credential.password)
            if cleanup_failed:
                raise ApplicationError(
                    "superadmin session cleanup failed",
                    type="SUPERADMIN_SESSION_CLEANUP_FAILED",
                    non_retryable=True,
                )

    @staticmethod
    def _login_path(project: str) -> str:
        return "/v1/auth/login-sessions" if project == "falcone" else "/api/v1/auth/login"

    @staticmethod
    def _login_body(credential: SuperadminCredential) -> dict[str, str]:
        if credential.project == "falcone":
            return {"username": credential.username, "password": credential.password}
        return {"email": credential.username, "password": credential.password}

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as error:
            raise ApplicationError("login response is not JSON", non_retryable=True) from error
        if not isinstance(value, dict):
            raise ApplicationError("login response is not an object", non_retryable=True)
        return value

    @classmethod
    def _session_material(cls, project: str, value: dict[str, Any]) -> list[str]:
        names = (
            (
                "accessToken",
                "refreshToken",
                "token",
                "access_token",
                "refresh_token",
                "csrfToken",
                "csrf_token",
            )
            if project == "falcone"
            else (
                "access_token",
                "refresh_token",
                "accessToken",
                "refreshToken",
                "csrfToken",
                "csrf_token",
            )
        )
        found: list[str] = []

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key in names and isinstance(nested, str) and nested:
                        found.append(nested)
                    elif isinstance(nested, (dict, list)):
                        walk(nested)
            elif isinstance(item, list):
                for nested in item:
                    walk(nested)

        walk(value)
        return found

    @staticmethod
    def _session_id(value: dict[str, Any]) -> str | None:
        for container in (value, value.get("data"), value.get("session")):
            if isinstance(container, dict):
                for key in ("sessionId", "session_id", "id"):
                    candidate = container.get(key)
                    if isinstance(candidate, str) and candidate:
                        return candidate
        return None

    @staticmethod
    def _refresh_token(value: dict[str, Any]) -> str | None:
        found: str | None = None

        def walk(item: Any) -> None:
            nonlocal found
            if found is not None:
                return
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key in {"refreshToken", "refresh_token"} and isinstance(nested, str):
                        if nested:
                            found = nested
                            return
                    elif isinstance(nested, (dict, list)):
                        walk(nested)
            elif isinstance(item, list):
                for nested in item:
                    walk(nested)

        walk(value)
        return found

    @staticmethod
    def _superadmin_role(value: dict[str, Any]) -> bool:
        roles: list[str] = []
        role_containers = {
            "role",
            "roles",
            "authority",
            "authorities",
            # Falcone's documented ConsoleSessionPrincipal field. Keep this
            # explicit: accepting arbitrary fields would weaken role proof.
            "platformroles",
            "platform_roles",
        }

        def walk(item: Any, key: str = "") -> None:
            if isinstance(item, dict):
                for nested_key, nested in item.items():
                    walk(nested, nested_key)
            elif isinstance(item, list):
                for nested in item:
                    walk(nested, key)
            elif isinstance(item, str) and key.lower() in role_containers:
                roles.append(item.lower())

        walk(value)
        return any(
            role in {"superadmin", "super_admin", "platform_admin", "platform-administrator"}
            for role in roles
        )

    async def _logout(self, session: EphemeralSuperadminSession) -> None:
        response: httpx.Response | None = None
        if session.project == "falcone" and session.session_id and session._refresh_token:
            response = await session.client.request(
                "DELETE",
                f"/v1/auth/login-sessions/{session.session_id}",
                json={"refreshToken": session._refresh_token},
                headers={"Cache-Control": "no-store"},
            )
        elif session.project == "musematic":
            response = await session.client.post("/api/v1/auth/logout")
        if response is not None:
            response.raise_for_status()
