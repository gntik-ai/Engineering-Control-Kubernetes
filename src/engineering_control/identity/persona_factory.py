from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from temporalio.exceptions import ApplicationError


class IdentityMode(StrEnum):
    CREATE_EPHEMERAL = "CREATE_EPHEMERAL"
    ACTIVATE_MANAGED_EXISTING = "ACTIVATE_MANAGED_EXISTING"


@dataclass(frozen=True)
class IdentityPolicy:
    resource_prefix: str = "e2e-"
    max_users_per_campaign: int = 8
    max_roles_per_campaign: int = 4
    default_ttl_minutes: int = 120


@dataclass(repr=False)
class EphemeralPersona:
    resource_name: str
    persona: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    role_id: str = field(repr=False)
    user_id: str = field(repr=False)
    created_role: bool
    activated_existing: bool
    access_token: str | None = field(default=None, repr=False)


class PersonaAdminClient(Protocol):
    async def discover_minimum_role(self, persona: str, scope: str) -> str | None: ...
    async def create_role(
        self, name: str, persona: str, scope: str, metadata: dict[str, str]
    ) -> str: ...
    async def find_managed_user(
        self, name: str, metadata: dict[str, str]
    ) -> dict[str, object] | None: ...
    async def create_user(self, name: str, password: str, metadata: dict[str, str]) -> str: ...
    async def snapshot_user(self, user_id: str) -> dict[str, object]: ...
    async def activate_user(self, user_id: str) -> None: ...
    async def assign(self, user_id: str, role_id: str, scope: str) -> None: ...
    async def authenticate(self, user_id: str, username: str, password: str) -> str | None: ...
    async def logout_persona(self, user_id: str) -> None: ...
    async def delete_user(self, user_id: str) -> None: ...
    async def restore_user(self, user_id: str, snapshot: dict[str, object]) -> None: ...
    async def delete_role(self, role_id: str) -> None: ...
    async def prove_absent_or_restored(
        self, user_id: str, snapshot: dict[str, object] | None
    ) -> bool: ...


class EphemeralPersonaFactory:
    """Create least-privilege campaign identities and always prove cleanup."""

    def __init__(self, client: PersonaAdminClient, policy: IdentityPolicy | None = None) -> None:
        self.client = client
        self.policy = policy or IdentityPolicy()
        self._users = 0
        self._roles = 0

    @asynccontextmanager
    async def persona(
        self,
        *,
        project: str,
        campaign_id: str,
        persona: str,
        scope: str,
        mode: IdentityMode = IdentityMode.CREATE_EPHEMERAL,
    ) -> AsyncIterator[EphemeralPersona]:
        self._validate(project, campaign_id, persona)
        if self._users >= self.policy.max_users_per_campaign:
            raise ApplicationError(
                "campaign user bound exceeded", type="PolicyViolation", non_retryable=True
            )
        suffix = secrets.token_hex(4)
        name = f"e2e-{project}-{campaign_id}-{persona}-{suffix}".lower()
        expires = datetime.now(UTC) + timedelta(minutes=self.policy.default_ttl_minutes)
        metadata = {
            "managed-by": "engineering-control",
            "campaign-id": campaign_id,
            "expires-at": expires.isoformat(),
            "persona": persona,
        }
        role_id = await self.client.discover_minimum_role(persona, scope)
        created_role = False
        if role_id is None:
            if self._roles >= self.policy.max_roles_per_campaign:
                raise ApplicationError(
                    "campaign role bound exceeded", type="PolicyViolation", non_retryable=True
                )
            try:
                role_id = await self.client.create_role(name, persona, scope, metadata)
            except NotImplementedError as error:
                raise ApplicationError(
                    "product cannot safely create the required minimum role",
                    type="IDENTITY_BOOTSTRAP_ROLE_UNSUPPORTED",
                    non_retryable=True,
                ) from error
            created_role = True
            self._roles += 1
        password = secrets.token_urlsafe(32)
        snapshot: dict[str, object] | None = None
        activated = False
        user_id = ""
        cleanup_complete = False
        generated: EphemeralPersona | None = None
        try:
            if mode is IdentityMode.ACTIVATE_MANAGED_EXISTING:
                found = await self.client.find_managed_user(name, metadata)
                if not found or found.get("managed-by") != "engineering-control":
                    raise ApplicationError(
                        "an unmanaged user cannot be activated",
                        type="PolicyViolation",
                        non_retryable=True,
                    )
                user_id = str(found["id"])
                snapshot = await self.client.snapshot_user(user_id)
                await self.client.activate_user(user_id)
                activated = True
            else:
                user_id = await self.client.create_user(name, password, metadata)
                self._users += 1
            await self.client.assign(user_id, role_id, scope)
            access_token = await self.client.authenticate(user_id, name, password)
            generated = EphemeralPersona(
                resource_name=name,
                persona=persona,
                username=name,
                password=password,
                role_id=role_id,
                user_id=user_id,
                created_role=created_role,
                activated_existing=activated,
                access_token=access_token,
            )
            yield generated
        finally:
            cleanup_errors: list[Exception] = []
            if user_id:
                try:
                    await self.client.logout_persona(user_id)
                except Exception as error:
                    cleanup_errors.append(error)
                try:
                    if activated and snapshot is not None:
                        await self.client.restore_user(user_id, snapshot)
                    elif not activated:
                        await self.client.delete_user(user_id)
                except Exception as error:
                    cleanup_errors.append(error)
                try:
                    cleanup_complete = await self.client.prove_absent_or_restored(user_id, snapshot)
                except Exception as error:
                    cleanup_errors.append(error)
            else:
                cleanup_complete = True
            if created_role and role_id:
                try:
                    await self.client.delete_role(role_id)
                except Exception as error:
                    cleanup_errors.append(error)
            if generated is not None:
                generated.password = ""
                generated.access_token = None
            if cleanup_errors or not cleanup_complete:
                cause = cleanup_errors[0] if cleanup_errors else None
                terminal_error = ApplicationError(
                    "temporary identity cleanup could not be proven",
                    type="INCOMPLETE_IDENTITY_CLEANUP",
                    non_retryable=True,
                )
                if cause is not None:
                    raise terminal_error from cause
                raise terminal_error

    @staticmethod
    def _validate(project: str, campaign_id: str, persona: str) -> None:
        if project not in {"falcone", "musematic", "llmwiki"}:
            raise ApplicationError("identity project is not allowlisted", non_retryable=True)
        if not re.fullmatch(r"[a-z0-9-]{1,80}", campaign_id):
            raise ApplicationError("campaign identity prefix is invalid", non_retryable=True)
        if persona not in {
            "standard-user",
            "privileged-admin",
            "platform-administrator",
            "developer-operator",
        }:
            raise ApplicationError("persona lane is not allowlisted", non_retryable=True)

    async def recover_stale(self, candidates: list[dict[str, str]]) -> list[str]:
        recovered: list[str] = []
        for item in candidates:
            name = item.get("name", "")
            if not re.fullmatch(
                r"e2e-(falcone|musematic|llmwiki)-[a-z0-9-]+-(standard-user|privileged-admin|platform-administrator|developer-operator)-[a-f0-9]{8}",
                name,
            ):
                continue
            if item.get("managed-by") != "engineering-control" or not item.get("campaign-id"):
                continue
            await self.client.delete_user(item["id"])
            recovered.append(name)
        return recovered
