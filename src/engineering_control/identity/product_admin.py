from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from temporalio.exceptions import ApplicationError

from engineering_control.identity.sessions import EphemeralSuperadminSession
from engineering_control.security.redaction import SecretRedactor

_BUILTIN_ROLES = {
    "standard-user": ("workspace_viewer", "viewer", "member", "workspace_developer"),
    "privileged-admin": ("workspace_admin", "tenant_admin", "admin"),
    "platform-administrator": ("platform_admin", "tenant_admin", "superadmin"),
    "developer-operator": ("workspace_operator", "operator", "developer"),
}


class HttpPersonaAdminClient:
    """Typed product admin API adapter used only inside one browser-worker execution."""

    def __init__(
        self,
        session: EphemeralSuperadminSession,
        *,
        tenant_id: str | None,
        workspace_id: str | None,
        redactor: SecretRedactor,
        identity_project: str | None = None,
        authentication_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.redactor = redactor
        self.identity_project = identity_project or session.project
        self.authentication_transport = authentication_transport
        self._persona_tokens: dict[str, str] = {}
        self._persona_passwords: dict[str, str] = {}

    async def discover_minimum_role(self, persona: str, scope: str) -> str | None:
        response = await self.session.client.get(self._roles_path())
        if response.status_code == 404:
            return None
        value = self._json(response, {200})
        roles = self._items(value)
        allowed = _BUILTIN_ROLES[persona]
        for preferred in allowed:
            for role in roles:
                name = str(role.get("name", role.get("slug", ""))).lower()
                if name == preferred and role.get("builtIn", role.get("built_in", True)) is True:
                    return name if self.session.project == "falcone" else str(role.get("id", name))
        return None

    async def create_role(
        self, name: str, persona: str, scope: str, metadata: dict[str, str]
    ) -> str:
        if self.session.project == "falcone":
            # Falcone's current role API cannot attach managed metadata. Fail closed
            # instead of creating a role that stale-resource recovery cannot identify.
            raise NotImplementedError("Falcone custom managed roles are unsupported")
        body = {
            "name": name,
            "permissions": list(_BUILTIN_ROLES[persona][:1]),
            "scope": scope,
            "metadata": metadata,
        }
        value = self._json(
            await self.session.client.post(self._roles_path(), json=body), {200, 201}
        )
        return self._id(value, "role")

    async def find_managed_user(
        self, name: str, metadata: dict[str, str]
    ) -> dict[str, object] | None:
        value = self._json(await self.session.client.get(self._users_path()), {200})
        for user in self._items(value):
            candidate_metadata = (
                user.get("attributes")
                if self.session.project == "falcone"
                else user.get("metadata")
            )
            if (
                user.get("username", user.get("email")) == name
                and isinstance(candidate_metadata, dict)
                and self._attribute(candidate_metadata, "managed-by") == "engineering-control"
                and self._attribute(candidate_metadata, "campaign-id") == metadata["campaign-id"]
            ):
                return {
                    **user,
                    "managed-by": "engineering-control",
                    "campaign-id": metadata["campaign-id"],
                }
        return None

    async def create_user(self, name: str, password: str, metadata: dict[str, str]) -> str:
        self.redactor.register_secret(password)
        body: dict[str, Any] = {
            "username": name,
            "email": f"{name}@invalid.example",
            "password": password,
            "enabled": True,
            "temporary": False,
        }
        if self.session.project == "falcone":
            body["attributes"] = {
                **metadata,
                **({"tenant_id": self.tenant_id} if self.tenant_id else {}),
                **({"workspace_id": self.workspace_id} if self.workspace_id else {}),
            }
        else:
            body["metadata"] = metadata
        try:
            value = self._json(
                await self.session.client.post(self._users_path(), json=body), {200, 201}
            )
            user_id = self._id(value, "user")
        except Exception:
            self.redactor.unregister_secret(password)
            raise
        self._persona_passwords[user_id] = password
        return user_id

    async def snapshot_user(self, user_id: str) -> dict[str, object]:
        return self._json(await self.session.client.get(f"{self._users_path()}/{user_id}"), {200})

    async def activate_user(self, user_id: str) -> None:
        path = f"{self._users_path()}/{user_id}"
        if self.session.project == "falcone":
            path += "/status"
        self._json(
            await self.session.client.patch(path, json={"enabled": True}),
            {200, 204},
        )

    async def assign(self, user_id: str, role_id: str, scope: str) -> None:
        if self.session.project == "falcone":
            self._json(
                await self.session.client.post(
                    f"{self._users_path()}/{user_id}/role-assignments",
                    json={"roles": [role_id]},
                ),
                {200, 201, 204},
            )
            return
        self._json(
            await self.session.client.post(
                f"{self._users_path()}/{user_id}/roles",
                json={
                    "roleId": role_id,
                    "scope": scope,
                    "tenantId": self.tenant_id,
                    "workspaceId": self.workspace_id,
                },
            ),
            {200, 201, 204},
        )

    async def authenticate(self, username: str, password: str) -> str | None:
        if self.identity_project == "llmwiki":
            # LLMWiki must authenticate through its browser authorization-code +
            # PKCE flow. The browser activity performs that bounded login; never
            # substitute a password-grant token here.
            return None
        temporary = httpx.AsyncClient(
            base_url=self.session.base_url,
            timeout=httpx.Timeout(30),
            follow_redirects=False,
            verify=True,
            transport=self.authentication_transport,
        )
        try:
            path = (
                "/v1/auth/login-sessions"
                if self.session.project == "falcone"
                else "/api/v1/auth/login"
            )
            key = "username" if self.session.project == "falcone" else "email"
            value = self._json(
                await temporary.post(path, json={key: username, "password": password}), {200, 201}
            )
            token = self._find_token(value)
            if token:
                self.redactor.register_secret(token)
                self._persona_tokens[username] = token
            return token
        finally:
            temporary.cookies.clear()
            await temporary.aclose()

    async def logout_persona(self, user_id: str) -> None:
        # Deleting/deactivating the identity invalidates its bounded protocol sessions.
        return None

    async def delete_user(self, user_id: str) -> None:
        response = await self.session.client.delete(f"{self._users_path()}/{user_id}")
        if response.status_code not in {200, 204, 404}:
            raise ApplicationError(
                "temporary user deletion failed", type="INCOMPLETE_IDENTITY_CLEANUP"
            )

    async def restore_user(self, user_id: str, snapshot: dict[str, object]) -> None:
        if self.session.project == "falcone":
            enabled = snapshot.get("enabled") is True
            self._json(
                await self.session.client.patch(
                    f"{self._users_path()}/{user_id}/status", json={"enabled": enabled}
                ),
                {200, 204},
            )
            snapshot_roles = snapshot.get("realmRoles", [])
            expected_roles = (
                {str(item) for item in snapshot_roles if isinstance(item, str)}
                if isinstance(snapshot_roles, list)
                else set()
            )
            current_value = self._json(
                await self.session.client.get(f"{self._users_path()}/{user_id}/roles"),
                {200},
            )
            current_roles = {
                str(item.get("name"))
                for item in self._items(current_value)
                if isinstance(item.get("name"), str)
            }
            added = sorted(expected_roles - current_roles)
            removed = sorted(current_roles - expected_roles)
            if added:
                self._json(
                    await self.session.client.post(
                        f"{self._users_path()}/{user_id}/role-assignments",
                        json={"roles": added},
                    ),
                    {200, 201, 204},
                )
            if removed:
                self._json(
                    await self.session.client.request(
                        "DELETE",
                        f"{self._users_path()}/{user_id}/role-assignments",
                        json={"roles": removed},
                    ),
                    {200, 204},
                )
            return
        self._json(
            await self.session.client.put(f"{self._users_path()}/{user_id}", json=snapshot),
            {200, 204},
        )

    async def delete_role(self, role_id: str) -> None:
        response = await self.session.client.delete(f"{self._roles_path()}/{role_id}")
        if response.status_code not in {200, 204, 404}:
            raise ApplicationError(
                "temporary role deletion failed", type="INCOMPLETE_IDENTITY_CLEANUP"
            )

    async def prove_absent_or_restored(
        self, user_id: str, snapshot: dict[str, object] | None
    ) -> bool:
        response = await self.session.client.get(f"{self._users_path()}/{user_id}")
        for token in self._persona_tokens.values():
            self.redactor.unregister_secret(token)
        self._persona_tokens.clear()
        password = self._persona_passwords.pop(user_id, None)
        if password:
            self.redactor.unregister_secret(password)
        return response.status_code in ({200} if snapshot is not None else {404, 410})

    async def list_stale_managed_resources(self) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        for kind, path in (("user", self._users_path()), ("role", self._roles_path())):
            response = await self.session.client.get(path)
            if response.status_code == 404:
                continue
            for item in self._items(self._json(response, {200})):
                metadata = (
                    item.get("attributes")
                    if self.session.project == "falcone"
                    else item.get("metadata")
                )
                name = str(item.get("username", item.get("name", "")))
                identifier = item.get("id")
                if (
                    not isinstance(metadata, dict)
                    or self._attribute(metadata, "managed-by") != "engineering-control"
                    or not isinstance(identifier, str)
                    or not self._managed_name(name)
                ):
                    continue
                expires_at = self._attribute(metadata, "expires-at")
                try:
                    expired = isinstance(expires_at, str) and datetime.fromisoformat(
                        expires_at
                    ).astimezone(UTC) <= datetime.now(UTC)
                except ValueError:
                    expired = False
                campaign_id = self._attribute(metadata, "campaign-id")
                if expired and isinstance(campaign_id, str) and campaign_id:
                    candidates.append(
                        {
                            "kind": kind,
                            "id": identifier,
                            "name": name,
                            "campaign-id": campaign_id,
                            "managed-by": "engineering-control",
                        }
                    )
        return candidates[:100]

    async def recover_stale_managed_resources(self, candidates: list[dict[str, str]]) -> list[str]:
        recovered: list[str] = []
        for kind in ("user", "role"):
            for item in candidates:
                if (
                    item.get("kind") != kind
                    or item.get("managed-by") != "engineering-control"
                    or not item.get("campaign-id")
                    or not self._managed_name(item.get("name", ""))
                ):
                    continue
                identifier = item.get("id", "")
                if not identifier or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", identifier):
                    continue
                if kind == "user":
                    await self.delete_user(identifier)
                else:
                    await self.delete_role(identifier)
                recovered.append(item["name"])
        return recovered

    @staticmethod
    def _managed_name(value: str) -> bool:
        return bool(
            re.fullmatch(
                r"e2e-(falcone|musematic|llmwiki)-[a-z0-9-]+-"
                r"(standard-user|privileged-admin|platform-administrator|developer-operator)-"
                r"[a-f0-9]{8}",
                value,
            )
        )

    def destroy_ephemeral_secrets(self) -> None:
        for value in (*self._persona_tokens.values(), *self._persona_passwords.values()):
            self.redactor.unregister_secret(value)
        self._persona_tokens.clear()
        self._persona_passwords.clear()

    def _roles_path(self) -> str:
        if self.session.project == "musematic":
            return "/api/v1/admin/roles"
        return f"/v1/iam/realms/{self._falcone_realm()}/roles"

    def _users_path(self) -> str:
        if self.session.project == "musematic":
            return "/api/v1/admin/users"
        return f"/v1/iam/realms/{self._falcone_realm()}/users"

    def _falcone_realm(self) -> str:
        if self.identity_project == "falcone":
            return "in-falcone-platform"
        if not self.tenant_id:
            raise ApplicationError(
                "Falcone identity bootstrap requires TENANT_ID",
                type="IDENTITY_BOOTSTRAP_SCOPE_MISSING",
            )
        return self.tenant_id

    @staticmethod
    def _attribute(values: dict[str, Any], name: str) -> str | None:
        value = values.get(name)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        return None

    @staticmethod
    def _json(response: httpx.Response, allowed: set[int]) -> dict[str, Any]:
        if response.status_code not in allowed:
            raise ApplicationError(
                "identity bootstrap API capability failed",
                type="IDENTITY_BOOTSTRAP_CAPABILITY_BLOCKED",
            )
        if response.status_code == 204:
            return {}
        try:
            value = response.json()
        except ValueError as error:
            raise ApplicationError(
                "identity API returned invalid JSON", non_retryable=True
            ) from error
        if not isinstance(value, dict):
            raise ApplicationError("identity API returned a non-object", non_retryable=True)
        return value

    @staticmethod
    def _items(value: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("items", "data", "users", "roles"):
            items = value.get(key)
            if isinstance(items, list) and all(isinstance(item, dict) for item in items):
                return items[:1000]
            if isinstance(items, dict):
                nested = items.get("items")
                if isinstance(nested, list) and all(isinstance(item, dict) for item in nested):
                    return nested[:1000]
        return []

    @staticmethod
    def _id(value: dict[str, Any], label: str) -> str:
        for candidate in (value, value.get("data")):
            if isinstance(candidate, dict):
                item = candidate.get("id", candidate.get("userId", candidate.get("roleName")))
                if isinstance(item, str) and item:
                    return item
        raise ApplicationError(f"{label} creation returned no identifier", non_retryable=True)

    @staticmethod
    def _find_token(value: dict[str, Any]) -> str | None:
        for candidate in (value, value.get("data"), value.get("session")):
            if isinstance(candidate, dict):
                for key in ("accessToken", "access_token", "token"):
                    token = candidate.get(key)
                    if isinstance(token, str) and token:
                        return token
        return None
