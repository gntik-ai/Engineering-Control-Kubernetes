from __future__ import annotations

import json
import secrets

import httpx
import pytest

from engineering_control.identity.persona_factory import EphemeralPersonaFactory
from engineering_control.identity.product_admin import HttpPersonaAdminClient
from engineering_control.identity.sessions import EphemeralSuperadminSession
from engineering_control.security.redaction import REDACTED, SecretRedactor


@pytest.mark.asyncio
async def test_falcone_persona_uses_platform_iam_contract_and_proves_cleanup() -> None:
    access_token = secrets.token_urlsafe(32)
    deleted = False
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deleted
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path.endswith("/roles"):
            return httpx.Response(
                200,
                json={"items": [{"id": "role-id", "name": "workspace_viewer"}]},
            )
        if request.method == "POST" and request.url.path.endswith("/users"):
            assert body is not None
            assert body["temporary"] is False
            assert "metadata" not in body
            assert body["attributes"]["managed-by"] == "engineering-control"  # type: ignore[index]
            assert body["attributes"]["tenant_id"] == "tenant-staging"  # type: ignore[index]
            assert body["attributes"]["workspace_id"] == "workspace-staging"  # type: ignore[index]
            return httpx.Response(201, json={"userId": "temporary-user-id"})
        if request.method == "POST" and request.url.path.endswith("/role-assignments"):
            assert body == {"roles": ["workspace_viewer"]}
            return httpx.Response(201, json={"assigned": ["workspace_viewer"]})
        if request.method == "POST" and request.url.path == "/v1/auth/login-sessions":
            return httpx.Response(201, json={"accessToken": access_token})
        if request.method == "DELETE" and request.url.path.endswith("/temporary-user-id"):
            deleted = True
            return httpx.Response(200, json={"deleted": True})
        if request.method == "GET" and request.url.path.endswith("/temporary-user-id"):
            return httpx.Response(404 if deleted else 200, json={})
        raise AssertionError((request.method, request.url.path))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="https://staging.example.invalid",
        transport=transport,
    )
    redactor = SecretRedactor()
    session = EphemeralSuperadminSession(
        project="falcone",
        base_url="https://staging.example.invalid",
        client=client,
    )
    admin = HttpPersonaAdminClient(
        session,
        tenant_id="tenant-staging",
        workspace_id="workspace-staging",
        redactor=redactor,
        identity_project="falcone",
        authentication_transport=transport,
    )
    factory = EphemeralPersonaFactory(admin)

    async with factory.persona(
        project="falcone",
        campaign_id="persona-canary",
        persona="standard-user",
        scope="workspace",
    ) as persona:
        assert persona.resource_name.startswith("e2e-falcone-persona-canary-standard-user-")
        assert persona.role_id == "workspace_viewer"
        assert persona.access_token == access_token
        assert redactor.redact_text(access_token).value == REDACTED

    assert deleted is True
    assert redactor.redact_text(access_token).value == access_token
    assert calls[0][1] == "/v1/iam/realms/in-falcone-platform/roles"
    assert all("/v1/tenants/" not in path for _, path, _ in calls)
    await client.aclose()


@pytest.mark.asyncio
async def test_llmwiki_persona_uses_tenant_realm_and_defers_auth_to_pkce_browser() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"items": [{"name": "workspace_viewer"}]})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="https://staging.example.invalid",
        transport=transport,
    )
    session = EphemeralSuperadminSession(
        project="falcone",
        base_url="https://staging.example.invalid",
        client=client,
    )
    admin = HttpPersonaAdminClient(
        session,
        tenant_id="llmwiki-realm",
        workspace_id="llmwiki-workspace",
        redactor=SecretRedactor(),
        identity_project="llmwiki",
        authentication_transport=transport,
    )

    assert await admin.discover_minimum_role("standard-user", "workspace") == "workspace_viewer"
    assert await admin.authenticate("ephemeral-user", "ephemeral-password") is None
    assert paths == ["/v1/iam/realms/llmwiki-realm/roles"]
    await client.aclose()


@pytest.mark.asyncio
async def test_falcone_refuses_unrecoverable_custom_role_creation() -> None:
    client = httpx.AsyncClient(base_url="https://staging.example.invalid")
    session = EphemeralSuperadminSession(
        project="falcone",
        base_url="https://staging.example.invalid",
        client=client,
    )
    admin = HttpPersonaAdminClient(
        session,
        tenant_id="tenant-staging",
        workspace_id="workspace-staging",
        redactor=SecretRedactor(),
        identity_project="falcone",
    )

    with pytest.raises(NotImplementedError):
        await admin.create_role(
            "e2e-falcone-campaign-standard-user-12345678",
            "standard-user",
            "workspace",
            {"managed-by": "engineering-control"},
        )
    await client.aclose()
