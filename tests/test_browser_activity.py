from __future__ import annotations

import contextlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.browser import BrowserActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings

ROOT = Path(__file__).parents[1]


class FakeBrowserRunner:
    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        assert executable == "node"
        environment = kwargs["environment"]
        output = Path(environment["ENGINEERING_JOURNEY_OUTPUT"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "error": None,
                    "console": [
                        {"type": "log", "text": f"unexpected {environment['JOURNEY_PASSWORD']}"}
                    ],
                    "network": [],
                    "visual_evidence": "DISABLED_CREDENTIAL_SAFETY",
                }
            ),
            encoding="utf-8",
        )
        return CommandResult(0, "", "")


class FailingBrowserRunner:
    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        assert executable == "node"
        return CommandResult(1, "", "synthetic runner failure")

    async def run_async(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        return self.run(executable, arguments, **kwargs)


@pytest.mark.asyncio
async def test_superadmin_attestation_maps_product_timeout_to_runtime_health_blocker(
    tmp_path: Path,
) -> None:
    credential_root = tmp_path / "credentials"
    credential = credential_root / "musematic" / "superadmin.env"
    credential.parent.mkdir(parents=True)
    credential.write_text(
        "\n".join(
            (
                "AUTH_METHOD=username_password",
                "BASE_URL=https://api.musematic.ai",
                "USERNAME=bounded-test-user",
                f"PASSWORD={secrets.token_urlsafe(32)}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("bounded product runtime timeout", request=request)

    handler = BrowserActivityHandler(
        Settings(
            project_config_root=ROOT / "config/projects",
            persona_config_root=ROOT / "config/personas",
            flow_config_root=ROOT / "config/flows",
            browser_credential_root=credential_root,
        ),
        api_transport=httpx.MockTransport(timeout),
    )

    result = await handler.handle("identity.superadmin_attest", {"project": "musematic"})

    assert result == {
        "project": "musematic",
        "contract_id": "musematic-superadmin",
        "file_present": True,
        "parser_valid": False,
        "target_valid": False,
        "authentication_valid": False,
        "superadmin_role_valid": False,
        "worker_identity": result["worker_identity"],
        "checked_timestamp": result["checked_timestamp"],
        "attestation_ttl_seconds": 300,
        "contract_hash": result["contract_hash"],
        "blocker_code": "BLOCKED_BY_RUNTIME_HEALTH",
    }


class _Credential:
    def __init__(self, project: str, base_url: str) -> None:
        self.project = project
        self.base_url = base_url
        self.tenant_id = "tenant"
        self.workspace_id = "workspace"


class _Sessions:
    """Scripted SuperadminSessionAdapter: per project, time out at login or yield a session."""

    def __init__(self, login_timeouts: set[str], exited: list[str]) -> None:
        self.login_timeouts = login_timeouts
        self.exited = exited

    def __call__(self, **_: Any) -> _Sessions:
        return self

    @contextlib.asynccontextmanager
    async def login(self, credential: _Credential) -> Any:
        if credential.project in self.login_timeouts:
            raise httpx.ReadTimeout("bounded product runtime timeout")
        try:
            yield credential.project
        finally:
            self.exited.append(credential.project)


class _Admin:
    """Scripted HttpPersonaAdminClient: per project, list candidates or time out."""

    destroyed: list[str] = []
    list_timeouts: set[str] = set()
    recover_timeouts: set[str] = set()

    def __init__(self, session: str, **_: Any) -> None:
        self.project = session

    async def list_stale_managed_resources(self) -> list[dict[str, str]]:
        if self.project in self.list_timeouts:
            raise httpx.ReadTimeout("bounded product runtime timeout")
        return [{"kind": "user", "id": f"{self.project}-stale-1"}]

    async def recover_stale_managed_resources(self, candidates: list[dict[str, str]]) -> list[str]:
        if self.project in self.recover_timeouts:
            raise httpx.ReadTimeout("bounded product runtime timeout")
        return [item["id"] for item in candidates]

    def destroy_ephemeral_secrets(self) -> None:
        self.destroyed.append(self.project)


def _identity_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    login_timeouts: set[str] = frozenset(),  # type: ignore[assignment]
    list_timeouts: set[str] = frozenset(),  # type: ignore[assignment]
    recover_timeouts: set[str] = frozenset(),  # type: ignore[assignment]
) -> tuple[BrowserActivityHandler, list[str]]:
    from engineering_control.activities import browser

    exited: list[str] = []
    urls = {"falcone": "https://baas.musematic.ai", "musematic": "https://api.musematic.ai"}

    class _Loader:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def load(self, project: str) -> _Credential:
            return _Credential(project, urls[project])

    _Admin.destroyed = []
    _Admin.list_timeouts = set(list_timeouts)
    _Admin.recover_timeouts = set(recover_timeouts)
    monkeypatch.setattr(browser, "SuperadminCredentialLoader", _Loader)
    monkeypatch.setattr(browser, "SuperadminSessionAdapter", _Sessions(set(login_timeouts), exited))
    monkeypatch.setattr(browser, "HttpPersonaAdminClient", _Admin)
    handler = BrowserActivityHandler(
        Settings(
            project_config_root=ROOT / "config/projects",
            persona_config_root=ROOT / "config/personas",
            flow_config_root=ROOT / "config/flows",
            browser_credential_root=tmp_path / "credentials",
        )
    )
    return handler, exited


@pytest.mark.asyncio
async def test_an_unreachable_product_blocks_only_its_own_identity_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # NEW-175: musematic staging stopped answering its login (2026-10-09/10). The ReadTimeout
    # escaped identity.inspect_stale and blocked every project's nightly cleanup.
    handler, _ = _identity_handler(monkeypatch, tmp_path, login_timeouts={"falcone"})

    result = await handler.handle("identity.inspect_stale", {"stale_identity_candidates": {}})

    assert result["status"] == "BLOCKED"
    assert result["identity_recovery_blockers"] == {"falcone": "BLOCKED_BY_RUNTIME_HEALTH"}
    # Processing continued after falcone's timeout: musematic was inspected normally.
    assert result["stale_identity_candidates"] == {
        "musematic": [{"kind": "user", "id": "musematic-stale-1"}]
    }


@pytest.mark.asyncio
async def test_a_timeout_after_login_still_destroys_the_session_and_blocks_one_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handler, exited = _identity_handler(monkeypatch, tmp_path, list_timeouts={"musematic"})

    result = await handler.handle("identity.inspect_stale", {"stale_identity_candidates": {}})

    assert result["identity_recovery_blockers"] == {"musematic": "BLOCKED_BY_RUNTIME_HEALTH"}
    assert result["stale_identity_candidates"] == {
        "falcone": [{"kind": "user", "id": "falcone-stale-1"}]
    }
    # Ephemeral admin secrets destroyed and the session closed for both projects.
    assert _Admin.destroyed == ["falcone", "musematic"]
    assert exited == ["falcone", "musematic"]


@pytest.mark.asyncio
async def test_a_transport_failure_during_recovery_still_fails_the_activity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Review: partial identity recovery must never let cleanup continue (recovery blockers
    # are not rescoped when inspection passed), so recovery keeps failing closed.
    handler, _ = _identity_handler(monkeypatch, tmp_path, recover_timeouts={"falcone"})
    payload = {
        "dry_run": False,
        "stale_identity_candidates": {"falcone": [{"kind": "user", "id": "falcone-stale-1"}]},
    }

    with pytest.raises(httpx.ReadTimeout):
        await handler.handle("identity.recover_stale", payload)
    assert _Admin.destroyed == ["falcone"]


def write_schema(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["start_url", "allowed_origins", "steps"],
            }
        ),
        encoding="utf-8",
    )


def write_plan(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "start_url": "https://preview.invalid/",
                "allowed_origins": ["https://preview.invalid"],
                "steps": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_browser_activity_rejects_persistent_persona_credentials(tmp_path: Path) -> None:
    plan_root = tmp_path / "plans"
    plan_root.mkdir()
    plan = plan_root / "journey.json"
    write_plan(plan)
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    credentials = credential_root / "persona.env"
    credentials.write_text("JOURNEY_PASSWORD=credential-value-for-test\n", encoding="utf-8")
    os.chmod(credentials, 0o600)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    script_root = tmp_path / "browser"
    script_root.mkdir()
    script = script_root / "run-journey.mjs"
    script.write_text("// fake", encoding="utf-8")
    schema = tmp_path / "journey.schema.json"
    write_schema(schema)
    handler = BrowserActivityHandler(
        Settings(
            journey_plan_root=plan_root,
            browser_credential_root=credential_root,
            browser_evidence_root=evidence_root,
            browser_script=script,
            journey_schema=schema,
        ),
        FakeBrowserRunner(),  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationError, match="persistent browser persona credentials"):
        await handler.handle(
            "runtime.browser_checks",
            {
                "journey_plan_path": str(plan),
                "credential_file": str(credentials),
                "output_directory": str(evidence_root / "run-id"),
            },
        )


@pytest.mark.asyncio
async def test_browser_activity_rejects_output_escape(tmp_path: Path) -> None:
    plan_root = tmp_path / "plans"
    plan_root.mkdir()
    plan = plan_root / "journey.json"
    write_plan(plan)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    script_root = tmp_path / "browser"
    script_root.mkdir()
    script = script_root / "run-journey.mjs"
    script.write_text("// fake", encoding="utf-8")
    schema = tmp_path / "journey.schema.json"
    write_schema(schema)
    handler = BrowserActivityHandler(
        Settings(
            journey_plan_root=plan_root,
            browser_evidence_root=evidence_root,
            browser_script=script,
            journey_schema=schema,
        ),
        FakeBrowserRunner(),  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationError, match="outside its evidence root"):
        await handler.handle(
            "runtime.browser_checks",
            {
                "journey_plan_path": str(plan),
                "output_directory": str(tmp_path / "outside"),
            },
        )


@pytest.mark.asyncio
async def test_browser_activity_never_reuses_stale_retry_artifacts(tmp_path: Path) -> None:
    plan_root = tmp_path / "plans"
    plan_root.mkdir()
    plan = plan_root / "journey.json"
    write_plan(plan)
    evidence_root = tmp_path / "evidence"
    output = evidence_root / "run-id"
    output.mkdir(parents=True)
    (output / "result.json").write_text('{"status":"PASS"}', encoding="utf-8")
    (output / "trace.zip").write_bytes(b"stale")
    script_root = tmp_path / "browser"
    script_root.mkdir()
    script = script_root / "run-journey.mjs"
    script.write_text("// fake", encoding="utf-8")
    schema = tmp_path / "journey.schema.json"
    write_schema(schema)
    handler = BrowserActivityHandler(
        Settings(
            journey_plan_root=plan_root,
            browser_evidence_root=evidence_root,
            browser_script=script,
            journey_schema=schema,
        ),
        FailingBrowserRunner(),  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationError, match="failed without a result"):
        await handler.handle(
            "runtime.browser_checks",
            {
                "journey_plan_path": str(plan),
                "output_directory": str(output),
            },
        )
    assert not (output / "result.json").exists()
    assert not (output / "trace.zip").exists()
