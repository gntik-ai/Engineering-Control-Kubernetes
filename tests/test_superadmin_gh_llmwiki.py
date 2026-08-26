from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, CommandResult
from engineering_control.adapters.github.client import (
    GhCliAuthError,
    GhCliBroker,
    GhCliPolicyError,
)
from engineering_control.adapters.llmwiki.staging import LLMWikiBootstrapPlanner
from engineering_control.api.schemas import AuditPlanRequest, LLMWikiBootstrapPlanRequest
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.engineering.github import FindingPublication
from engineering_control.identity.attestation import (
    SuperadminReadinessAttestation,
    superadmin_contract_hash,
)
from engineering_control.identity.credentials import SuperadminCredential
from engineering_control.identity.persona_factory import EphemeralPersonaFactory, IdentityMode
from engineering_control.identity.sessions import SuperadminSessionAdapter
from engineering_control.models.enums import Project
from engineering_control.security.redaction import REDACTED, SecretRedactor

ROOT = Path(__file__).parents[1]


def _synthetic_musematic_access_token(roles: list[dict[str, str] | str]) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        (
            encode({"alg": "RS256", "typ": "JWT"}),
            encode({"roles": roles, "session_id": "bounded-session"}),
            "synthetic-signature",
        )
    )


def test_exact_persistent_product_credential_contracts() -> None:
    value = yaml.safe_load((ROOT / "config/credentials/contracts.yaml").read_text())
    contracts = value["contracts"]
    product = {name: item for name, item in contracts.items() if name.endswith("-superadmin")}
    assert set(product) == {"falcone-superadmin", "musematic-superadmin"}
    assert all(
        item["required_keys"] == ["AUTH_METHOD", "BASE_URL", "USERNAME", "PASSWORD"]
        for item in product.values()
    )
    assert "llmwiki-superadmin" not in contracts
    assert not any("standard-user" in name or "privileged-admin" in name for name in contracts)
    serialized = json.dumps(value).lower()
    assert "access-token" not in serialized
    assert "refresh-token" not in serialized


def test_every_product_persona_is_runtime_generated() -> None:
    for project in ("falcone", "musematic", "llmwiki"):
        value = yaml.safe_load((ROOT / f"config/personas/{project}.yaml").read_text())
        authenticated = [item for item in value["personas"] if item["id"] != "anonymous"]
        assert authenticated
        assert all(item["credential_ref"] is None for item in authenticated)
        assert all(item["identity_source"].startswith("ephemeral-") for item in authenticated)
    llmwiki = yaml.safe_load((ROOT / "config/personas/llmwiki.yaml").read_text())
    assert llmwiki["persistent_contract"] is None


class _GhRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        **_: Any,
    ) -> CommandResult:
        self.calls.append((executable, arguments, environment))
        assert cwd.is_dir()
        if arguments[:2] == ["auth", "status"]:
            return CommandResult(0, "", "")
        if arguments[:2] == ["api", "user"]:
            return CommandResult(0, '{"login":"operator","type":"User"}', "")
        if arguments[:2] == ["repo", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "nameWithOwner": arguments[2],
                        "defaultBranchRef": {"name": "master"},
                        "viewerPermission": "ADMIN",
                        "isPrivate": False,
                    }
                ),
                "",
            )
        if arguments[:2] == ["issue", "list"]:
            return CommandResult(0, "[]", "")
        raise AssertionError(arguments)


class _ExpiredGhRunner(_GhRunner):
    def run(self, *args: Any, **kwargs: Any) -> CommandResult:
        raise CommandFailure("gh authentication unavailable")


def test_gh_cli_broker_requires_auth_identity_and_allowlisted_repository(tmp_path: Path) -> None:
    runner = _GhRunner()
    broker = GhCliBroker(runner, tmp_path, authenticated_home=tmp_path)
    assert broker.search_issues("gntik-ai/llmwiki", "consumer contract") == []
    assert runner.calls
    assert {call[0] for call in runner.calls} == {"gh"}
    assert any(call[1][:2] == ["auth", "status"] for call in runner.calls)
    assert any(call[1][:2] == ["api", "user"] for call in runner.calls)
    assert all("GH_TOKEN" not in call[2] for call in runner.calls)
    with pytest.raises(GhCliPolicyError):
        broker.search_issues("someone/unapproved", "contract")
    with pytest.raises(GhCliPolicyError):
        broker.search_issues("gntik-ai/llmwiki", "$(touch /tmp/blocked)")
    with pytest.raises(GhCliPolicyError):
        broker._run(["auth", "token"])


def test_gh_cli_writes_and_read_only_companion_are_fail_closed(tmp_path: Path) -> None:
    broker = GhCliBroker(_GhRunner(), tmp_path, authenticated_home=tmp_path)
    body = tmp_path / "body.md"
    body.write_text("bounded", encoding="utf-8")
    with pytest.raises(GhCliPolicyError, match="disabled"):
        broker.create_issue(
            repository="gntik-ai/llmwiki",
            title="candidate",
            body_file=body,
            change_binding="finding:12345678",
        )


def test_gh_cli_auth_expiry_is_a_typed_workflow_blocker(tmp_path: Path) -> None:
    broker = GhCliBroker(_ExpiredGhRunner(), tmp_path, authenticated_home=tmp_path)
    with pytest.raises(GhCliAuthError):
        broker.preflight(("gntik-ai/llmwiki",))
    body = tmp_path / "body.md"
    body.write_text("bounded", encoding="utf-8")
    write_broker = GhCliBroker(
        _GhRunner(), tmp_path, authenticated_home=tmp_path, writes_enabled=True
    )
    with pytest.raises(GhCliPolicyError, match="read-only"):
        write_broker.create_issue(
            repository="gntik-ai/llmwiki-contracts",
            title="candidate",
            body_file=body,
            change_binding="finding:12345678",
        )


@pytest.mark.asyncio
async def test_superadmin_session_material_is_ephemeral_and_redacted() -> None:
    secret = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    logout_bodies: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "accessToken": secret,
                    "refreshToken": refresh,
                    "sessionId": "bounded-session",
                    "roles": ["superadmin"],
                },
            )
        logout_bodies.append(json.loads(request.content))
        return httpx.Response(204)

    redactor = SecretRedactor()
    adapter = SuperadminSessionAdapter(redactor=redactor, transport=httpx.MockTransport(handler))
    credential = SuperadminCredential(
        project="falcone",
        base_url="https://staging.example.invalid",
        username="superadmin-test",
        password=secrets.token_urlsafe(32),
    )
    retained = None
    async with adapter.login(credential) as session:
        retained = session
        assert session.safe_summary() == {
            "project": "falcone",
            "authentication_valid": True,
            "superadmin_role_valid": True,
        }
        assert redactor.redact_text(secret).value == REDACTED
        assert session.client.headers["Authorization"] == f"Bearer {secret}"
    assert retained is not None
    assert retained.session_id is None
    assert retained._refresh_token is None
    assert retained._secrets == []
    assert "Authorization" not in retained.client.headers
    assert redactor.redact_text(secret).value == secret
    assert logout_bodies == [{"refreshToken": refresh}]


@pytest.mark.asyncio
async def test_falcone_documented_platform_roles_prove_superadmin() -> None:
    secret = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "sessionId": "bounded-session",
                    "tokenSet": {"accessToken": secret, "refreshToken": refresh},
                    "principal": {"platformRoles": ["superadmin"]},
                },
            )
        return httpx.Response(200, json={"status": "accepted"})

    adapter = SuperadminSessionAdapter(
        redactor=SecretRedactor(), transport=httpx.MockTransport(handler)
    )
    credential = SuperadminCredential(
        project="falcone",
        base_url="https://staging.example.invalid",
        username="superadmin-test",
        password=secrets.token_urlsafe(32),
    )

    async with adapter.login(credential) as session:
        assert session.superadmin_role_valid is True


@pytest.mark.asyncio
async def test_falcone_platform_roles_reject_non_superadmin() -> None:
    secret = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "sessionId": "bounded-session",
                    "tokenSet": {"accessToken": secret, "refreshToken": refresh},
                    "principal": {"platformRoles": ["tenant_admin"]},
                },
            )
        return httpx.Response(200, json={"status": "accepted"})

    adapter = SuperadminSessionAdapter(
        redactor=SecretRedactor(), transport=httpx.MockTransport(handler)
    )
    credential = SuperadminCredential(
        project="falcone",
        base_url="https://staging.example.invalid",
        username="tenant-admin-test",
        password=secrets.token_urlsafe(32),
    )

    with pytest.raises(ApplicationError) as error:
        async with adapter.login(credential):
            pass
    assert error.value.type == "SUPERADMIN_ROLE_INVALID"


@pytest.mark.asyncio
async def test_falcone_logout_failure_is_a_terminal_cleanup_blocker() -> None:
    secret = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    redactor = SecretRedactor()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "sessionId": "bounded-session",
                    "tokenSet": {"accessToken": secret, "refreshToken": refresh},
                    "principal": {"platformRoles": ["superadmin"]},
                },
            )
        return httpx.Response(503)

    adapter = SuperadminSessionAdapter(redactor=redactor, transport=httpx.MockTransport(handler))
    credential = SuperadminCredential(
        project="falcone",
        base_url="https://staging.example.invalid",
        username="superadmin-test",
        password=secrets.token_urlsafe(32),
    )
    retained = None
    with pytest.raises(ApplicationError) as error:
        async with adapter.login(credential) as session:
            retained = session
    assert error.value.type == "SUPERADMIN_SESSION_CLEANUP_FAILED"
    assert retained is not None
    assert retained.session_id is None
    assert retained._refresh_token is None
    assert retained._secrets == []
    assert "Authorization" not in retained.client.headers
    assert redactor.redact_text(secret).value == secret


@pytest.mark.asyncio
async def test_musematic_uses_explicit_access_token_for_identity_and_logout() -> None:
    access = _synthetic_musematic_access_token(
        [{"role": "superadmin", "workspace_id": "bounded-workspace"}]
    )
    refresh = secrets.token_urlsafe(32)
    redactor = SecretRedactor()
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v1/auth/login":
            # Deliberately put refresh_token first: JSON traversal order must not
            # decide which bearer authenticates the identity and logout calls.
            return httpx.Response(
                200,
                json={"refresh_token": refresh, "access_token": access},
            )
        assert request.headers["Authorization"] == f"Bearer {access}"
        if request.method == "GET" and request.url.path == "/api/v1/accounts/me":
            return httpx.Response(200, json={"id": "bounded-identity"})
        if request.method == "POST" and request.url.path == "/api/v1/auth/logout":
            return httpx.Response(200, json={"status": "logged_out"})
        raise AssertionError((request.method, request.url.path))

    adapter = SuperadminSessionAdapter(redactor=redactor, transport=httpx.MockTransport(handler))
    credential = SuperadminCredential(
        project="musematic",
        base_url="https://staging.example.invalid",
        username="superadmin-test",
        password=secrets.token_urlsafe(32),
    )
    retained = None
    async with adapter.login(credential) as session:
        retained = session
        assert session.superadmin_role_valid is True
        assert session.client.headers["Authorization"] == f"Bearer {access}"
        assert redactor.redact_text(refresh).value == REDACTED
        assert access not in repr(session)
        assert access not in json.dumps(session.safe_summary())

    assert retained is not None
    assert retained._access_token is None
    assert retained._refresh_token is None
    assert retained._secrets == []
    assert "Authorization" not in retained.client.headers
    assert redactor.redact_text(refresh).value == refresh
    assert calls == [
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/accounts/me"),
        ("POST", "/api/v1/auth/logout"),
    ]


@pytest.mark.asyncio
async def test_musematic_rejects_non_superadmin_claim_after_server_identity_validation() -> None:
    access = _synthetic_musematic_access_token([{"role": "platform_admin"}])
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": access})
        assert request.headers["Authorization"] == f"Bearer {access}"
        if request.url.path == "/api/v1/accounts/me":
            return httpx.Response(200, json={"id": "bounded-identity"})
        return httpx.Response(200, json={"status": "logged_out"})

    adapter = SuperadminSessionAdapter(
        redactor=SecretRedactor(), transport=httpx.MockTransport(handler)
    )
    credential = SuperadminCredential(
        project="musematic",
        base_url="https://staging.example.invalid",
        username="platform-admin-test",
        password=secrets.token_urlsafe(32),
    )

    with pytest.raises(ApplicationError) as error:
        async with adapter.login(credential):
            pass
    assert error.value.type == "SUPERADMIN_ROLE_INVALID"
    assert paths == [
        "/api/v1/auth/login",
        "/api/v1/accounts/me",
        "/api/v1/auth/logout",
    ]


@pytest.mark.asyncio
async def test_musematic_requires_server_accepted_access_token_before_role_proof() -> None:
    access = _synthetic_musematic_access_token([{"role": "superadmin"}])
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": access})
        if request.url.path == "/api/v1/accounts/me":
            return httpx.Response(401, json={"detail": "denied"})
        return httpx.Response(200, json={"status": "logged_out"})

    adapter = SuperadminSessionAdapter(
        redactor=SecretRedactor(), transport=httpx.MockTransport(handler)
    )
    credential = SuperadminCredential(
        project="musematic",
        base_url="https://staging.example.invalid",
        username="superadmin-test",
        password=secrets.token_urlsafe(32),
    )

    with pytest.raises(ApplicationError) as error:
        async with adapter.login(credential):
            pass
    assert error.value.type == "SUPERADMIN_AUTHENTICATION_FAILED"
    assert paths == [
        "/api/v1/auth/login",
        "/api/v1/accounts/me",
        "/api/v1/auth/logout",
    ]


@pytest.mark.asyncio
async def test_musematic_logout_failure_clears_local_material_and_blocks_readiness() -> None:
    access = _synthetic_musematic_access_token([{"role": "superadmin"}])
    refresh = secrets.token_urlsafe(32)
    redactor = SecretRedactor()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200,
                json={"refresh_token": refresh, "access_token": access},
            )
        if request.url.path == "/api/v1/accounts/me":
            return httpx.Response(200, json={"id": "bounded-identity"})
        return httpx.Response(401, json={"detail": "logout rejected"})

    adapter = SuperadminSessionAdapter(redactor=redactor, transport=httpx.MockTransport(handler))
    credential = SuperadminCredential(
        project="musematic",
        base_url="https://staging.example.invalid",
        username="superadmin-test",
        password=secrets.token_urlsafe(32),
    )
    retained = None
    with pytest.raises(ApplicationError) as error:
        async with adapter.login(credential) as session:
            retained = session
    assert error.value.type == "SUPERADMIN_SESSION_CLEANUP_FAILED"
    assert retained is not None
    assert retained._access_token is None
    assert retained._refresh_token is None
    assert retained._secrets == []
    assert "Authorization" not in retained.client.headers
    assert redactor.redact_text(refresh).value == refresh


class _PersonaAdmin:
    def __init__(
        self,
        *,
        cleanup_complete: bool = True,
        unmanaged: bool = False,
        role_missing: bool = False,
        logout_fails: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.cleanup_complete = cleanup_complete
        self.unmanaged = unmanaged
        self.role_missing = role_missing
        self.logout_fails = logout_fails

    async def discover_minimum_role(self, persona: str, scope: str) -> str | None:
        self.events.append("discover")
        return None if self.role_missing else "minimum-role"

    async def create_role(
        self, name: str, persona: str, scope: str, metadata: dict[str, str]
    ) -> str:
        self.events.append("create-role")
        return "created-role"

    async def find_managed_user(
        self, name: str, metadata: dict[str, str]
    ) -> dict[str, object] | None:
        return {
            "id": "existing",
            "managed-by": "human" if self.unmanaged else "engineering-control",
        }

    async def create_user(self, name: str, password: str, metadata: dict[str, str]) -> str:
        assert name.startswith("e2e-falcone-campaign-1-standard-user-")
        assert metadata["managed-by"] == "engineering-control"
        self.events.append("create-user")
        return "created-user"

    async def snapshot_user(self, user_id: str) -> dict[str, object]:
        return {"enabled": False, "roles": []}

    async def activate_user(self, user_id: str) -> None:
        self.events.append("activate")

    async def assign(self, user_id: str, role_id: str, scope: str) -> None:
        self.events.append("assign")

    async def authenticate(self, user_id: str, username: str, password: str) -> str | None:
        return "ephemeral-persona-token"

    async def logout_persona(self, user_id: str) -> None:
        self.events.append("logout")
        if self.logout_fails:
            raise RuntimeError("synthetic logout failure")

    async def delete_user(self, user_id: str) -> None:
        self.events.append("delete-user")

    async def restore_user(self, user_id: str, snapshot: dict[str, object]) -> None:
        self.events.append("restore")

    async def delete_role(self, role_id: str) -> None:
        self.events.append("delete-role")

    async def prove_absent_or_restored(
        self, user_id: str, snapshot: dict[str, object] | None
    ) -> bool:
        self.events.append("prove")
        return self.cleanup_complete


@pytest.mark.asyncio
async def test_persona_factory_prefixes_and_cleans_after_success_and_failure() -> None:
    success = _PersonaAdmin()
    generated = None
    async with EphemeralPersonaFactory(success).persona(
        project="falcone",
        campaign_id="campaign-1",
        persona="standard-user",
        scope="workspace-a",
    ) as persona:
        generated = persona
        assert persona.resource_name.startswith("e2e-falcone-campaign-1-standard-user-")
    assert success.events[-3:] == ["logout", "delete-user", "prove"]
    assert generated is not None
    assert generated.password == ""
    assert generated.access_token is None
    assert "create-role" not in success.events

    failed = _PersonaAdmin()
    with pytest.raises(RuntimeError, match="flow failed"):
        async with EphemeralPersonaFactory(failed).persona(
            project="falcone",
            campaign_id="campaign-1",
            persona="standard-user",
            scope="workspace-a",
        ):
            raise RuntimeError("flow failed")
    assert failed.events[-3:] == ["logout", "delete-user", "prove"]


@pytest.mark.asyncio
async def test_persona_factory_attempts_identity_deletion_when_logout_fails() -> None:
    admin = _PersonaAdmin(logout_fails=True)

    with pytest.raises(ApplicationError) as caught:
        async with EphemeralPersonaFactory(admin).persona(
            project="falcone",
            campaign_id="campaign-1",
            persona="standard-user",
            scope="workspace-a",
        ):
            pass

    assert caught.value.type == "INCOMPLETE_IDENTITY_CLEANUP"
    assert admin.events[-3:] == ["logout", "delete-user", "prove"]


@pytest.mark.asyncio
async def test_persona_factory_creates_and_removes_custom_role_only_when_required() -> None:
    client = _PersonaAdmin(role_missing=True)
    async with EphemeralPersonaFactory(client).persona(
        project="falcone",
        campaign_id="campaign-1",
        persona="standard-user",
        scope="workspace-a",
    ):
        pass
    assert client.events.count("create-role") == 1
    assert client.events.count("delete-role") == 1


@pytest.mark.asyncio
async def test_persona_factory_cleans_on_cancellation_and_blocks_incomplete_cleanup() -> None:
    cancelled = _PersonaAdmin()
    with pytest.raises(asyncio.CancelledError):
        async with EphemeralPersonaFactory(cancelled).persona(
            project="falcone",
            campaign_id="campaign-1",
            persona="standard-user",
            scope="workspace-a",
        ):
            raise asyncio.CancelledError
    assert cancelled.events[-3:] == ["logout", "delete-user", "prove"]

    incomplete = _PersonaAdmin(cleanup_complete=False)
    with pytest.raises(ApplicationError) as captured:
        async with EphemeralPersonaFactory(incomplete).persona(
            project="falcone",
            campaign_id="campaign-1",
            persona="standard-user",
            scope="workspace-a",
        ):
            pass
    assert captured.value.type == "INCOMPLETE_IDENTITY_CLEANUP"


@pytest.mark.asyncio
async def test_persona_factory_cleans_after_timeout() -> None:
    client = _PersonaAdmin()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            async with EphemeralPersonaFactory(client).persona(
                project="falcone",
                campaign_id="campaign-1",
                persona="standard-user",
                scope="workspace-a",
            ):
                await asyncio.sleep(60)
    assert client.events[-3:] == ["logout", "delete-user", "prove"]


@pytest.mark.asyncio
async def test_persona_factory_refuses_unmanaged_activation() -> None:
    client = _PersonaAdmin(unmanaged=True)
    with pytest.raises(ApplicationError, match="unmanaged"):
        async with EphemeralPersonaFactory(client).persona(
            project="falcone",
            campaign_id="campaign-1",
            persona="standard-user",
            scope="workspace-a",
            mode=IdentityMode.ACTIVATE_MANAGED_EXISTING,
        ):
            pass
    assert "activate" not in client.events


@pytest.mark.asyncio
async def test_managed_existing_user_is_restored_and_not_deleted() -> None:
    client = _PersonaAdmin()
    async with EphemeralPersonaFactory(client).persona(
        project="falcone",
        campaign_id="campaign-1",
        persona="standard-user",
        scope="workspace-a",
        mode=IdentityMode.ACTIVATE_MANAGED_EXISTING,
    ):
        pass
    assert "activate" in client.events
    assert "restore" in client.events
    assert "delete-user" not in client.events


@pytest.mark.asyncio
async def test_stale_recovery_after_worker_restart_matches_only_managed_resources() -> None:
    client = _PersonaAdmin()
    factory = EphemeralPersonaFactory(client)
    recovered = await factory.recover_stale(
        [
            {
                "id": "managed",
                "name": "e2e-falcone-campaign-1-standard-user-0123abcd",
                "managed-by": "engineering-control",
                "campaign-id": "campaign-1",
            },
            {
                "id": "human",
                "name": "human-disabled-user",
                "managed-by": "human",
                "campaign-id": "campaign-1",
            },
        ]
    )
    assert recovered == ["e2e-falcone-campaign-1-standard-user-0123abcd"]
    assert client.events.count("delete-user") == 1


def test_worker_attestation_is_bounded_and_contains_no_identity_or_secret() -> None:
    value = SuperadminReadinessAttestation(
        project="falcone",
        contract_id="falcone-superadmin",
        file_present=True,
        parser_valid=True,
        target_valid=True,
        authentication_valid=True,
        superadmin_role_valid=True,
        worker_identity="browser-runner@worker",
        checked_timestamp=datetime.now(UTC),
        attestation_ttl_seconds=300,
        contract_hash=superadmin_contract_hash("falcone"),
        blocker_code="NONE",
    ).model_dump(mode="json")
    serialized = json.dumps(value).lower()
    assert "username" not in serialized
    assert "password" not in serialized
    assert "token" not in serialized
    assert "cookie" not in serialized


def test_llmwiki_is_staging_only_self_deployed_and_falcone_bound() -> None:
    catalog = EngineeringCatalog(
        project_root=ROOT / "config/projects",
        persona_root=ROOT / "config/personas",
        flow_root=ROOT / "config/flows",
    )
    manifest = catalog.project("llmwiki")
    assert str(manifest.repositories.source.full_name) == "gntik-ai/llmwiki"
    assert manifest.repositories.source.default_branch == "master"
    assert manifest.repositories.source.full_name == manifest.repositories.deployment.full_name
    assert manifest.deployment.strategy == "repo-local-manifest"
    assert manifest.deployment.environment_policy == "staging-only"
    assert manifest.deployment.production_supported is False
    assert manifest.deployment.production_installed is False
    assert manifest.dependencies["falcone"]["environment"] == "staging"  # type: ignore[index]
    with pytest.raises(ValidationError):
        AuditPlanRequest(project=Project.LLMWIKI, preset="e2e-smoke", environment="production")
    with pytest.raises(ValidationError):
        LLMWikiBootstrapPlanRequest(environment="production")  # type: ignore[arg-type]


def test_llmwiki_bootstrap_is_idempotent_public_client_and_secret_free() -> None:
    values = dict(
        source_sha="a" * 40,
        falcone_source_sha="b" * 40,
        falcone_deployment_sha="c" * 40,
        falcone_url_resolved=False,
        public_origin_resolved=False,
        image_pull_ready=False,
    )
    first = LLMWikiBootstrapPlanner.plan(**values)
    second = LLMWikiBootstrapPlanner.plan(**values)
    assert first == second
    assert first.approval_binding() == second.approval_binding()
    assert first.oidc_client_type == "public"
    assert first.oidc_client_secret is None
    assert first.production_supported is False
    names = {item.name for item in first.required_values}
    assert not names & {"FALCONE_SUPERADMIN_PASSWORD", "FALCONE_ACCESS_TOKEN", "OIDC_CLIENT_SECRET"}
    assert set(first.blockers) == {
        "BLOCKED_BY_FALCONE_API_URL_DISCREPANCY",
        "BLOCKED_BY_STAGING_PUBLIC_ORIGIN",
        "BLOCKED_BY_IMAGE_PULL_AUTH",
    }
    assert len({action.idempotency_key for action in first.actions}) == len(first.actions)


def test_falcone_intensive_preset_includes_llmwiki_consumer_contract() -> None:
    flow = yaml.safe_load((ROOT / "config/flows/falcone/llmwiki-consumer.yaml").read_text())
    assert flow["id"] == "llmwiki-consumer-contracts"
    assert flow["mutation_class"] == "IDENTITY_BOOTSTRAP"
    definitions = (ROOT / "src/engineering_control/workflows/definitions.py").read_text()
    assert 'if request.preset == "falcone-intensive"' in definitions
    assert '"audit.llmwiki_consumer_suite"' in definitions


def test_llmwiki_and_consumer_gap_finding_routing() -> None:
    common: dict[str, Any] = {
        "source_sha": "a" * 40,
        "deployment_sha": "b" * 40,
        "running_revision": f"sha256:{'c' * 64}",
        "persona": "standard-user",
        "flow": "consumer-contract",
        "expected_behavior": "contract works",
        "actual_behavior": "contract fails",
        "minimal_reproduction": ["run bounded contract"],
        "evidence_handles": [f"evidence:{'d' * 64}"],
        "severity": "medium",
        "risk": "R2",
        "fingerprint": "finding-1234567890abcdef",
        "acceptance_criteria": ["consumer passes"],
        "cleanup_result": "PROVEN",
    }
    portal = FindingPublication(
        project="llmwiki",
        classification="application",
        affected_repositories=["gntik-ai/llmwiki"],
        **common,
    )
    assert portal.primary_repository() == "gntik-ai/llmwiki"
    gap = FindingPublication(
        project="llmwiki",
        classification="falcone-contract-gap",
        affected_repositories=[
            "gntik-ai/falcone",
            "gntik-ai/falcone-charts",
            "gntik-ai/llmwiki",
        ],
        falcone_source_sha="e" * 40,
        falcone_charts_sha="f" * 40,
        llmwiki_sha="1" * 40,
        consumer_contract="OIDC-PKCE",
        staging_target="llmwiki-e2e",
        **common,
    )
    assert gap.primary_repository() == "gntik-ai/falcone"


def test_incomplete_identity_cleanup_cannot_form_a_publication() -> None:
    with pytest.raises(ValidationError):
        FindingPublication(
            project="llmwiki",
            classification="application",
            affected_repositories=["gntik-ai/llmwiki"],
            source_sha="a" * 40,
            deployment_sha="b" * 40,
            running_revision=f"sha256:{'c' * 64}",
            persona="standard-user",
            flow="consumer-contract",
            expected_behavior="contract works",
            actual_behavior="contract fails",
            minimal_reproduction=["run bounded contract"],
            evidence_handles=[f"evidence:{'d' * 64}"],
            severity="medium",
            risk="R2",
            fingerprint="finding-1234567890abcdef",
            acceptance_criteria=["consumer passes"],
            cleanup_result="INCOMPLETE_IDENTITY_CLEANUP",
        )


def test_persona_factory_has_no_built_in_role_or_human_user_mutation_surface() -> None:
    methods = set(EphemeralPersonaFactory.__dict__)
    assert "modify_builtin_role" not in methods
    assert "modify_user" not in methods
    assert "activate_user" not in methods


def test_no_active_github_token_contract_or_forbidden_fallback() -> None:
    active_paths = [ROOT / "src", ROOT / "config", ROOT / "scripts", ROOT / "systemd"]
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for root in active_paths
        for path in root.rglob("*")
        if path.is_file()
    ).lower()
    for name in (
        "controller-read.token",
        "github-read.token",
        "delivery-read.token",
        "issues-write.token",
        "github-pr-write.token",
    ):
        assert name not in text
    assert "openrouter" not in text
    control = yaml.safe_load((ROOT / "config/control.yaml").read_text())
    assert control["paid_api_fallback"] == {"enabled": False}
    process_adapter = (ROOT / "src/engineering_control/adapters/process.py").read_text()
    assert "OPENAI_API_KEY" in process_adapter
    assert "ANTHROPIC_API_KEY" in process_adapter
    assert "env.pop(key, None)" in process_adapter
