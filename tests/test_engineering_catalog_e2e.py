from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from engineering_control.activities.browser import BrowserActivityHandler
from engineering_control.config import Settings
from engineering_control.engineering.configuration import EngineeringCatalog

ROOT = Path(__file__).parents[1]


def settings(tmp_path: Path) -> Settings:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return Settings(
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
        browser_credential_root=credentials,
        browser_evidence_root=evidence,
        temporary_root=tmp_path / "temporary",
        journey_schema=ROOT / "schemas/journey-plan.schema.json",
        browser_script=ROOT / "browser/run-journey.mjs",
    )


def test_pair_persona_and_flow_catalogs_are_strict_and_source_backed(tmp_path: Path) -> None:
    configuration = settings(tmp_path)
    catalog = EngineeringCatalog(
        project_root=configuration.project_config_root,
        persona_root=configuration.persona_config_root,
        flow_root=configuration.flow_config_root,
        credential_root=configuration.credential_root,
    )

    falcone = catalog.project("falcone")
    musematic = catalog.project("musematic")
    assert str(falcone.repositories.deployment.full_name) == "gntik-ai/falcone-charts"
    assert str(musematic.repositories.deployment.full_name) == "gntik-ai/musematic-deploy"
    assert str(falcone.api_url) == "https://baas.musematic.ai/"
    assert str(musematic.api_url) == "https://api.musematic.ai/"
    llmwiki = catalog.project("llmwiki")
    assert str(llmwiki.repositories.source.full_name) == "gntik-ai/llmwiki"
    assert str(llmwiki.repositories.deployment.full_name) == "gntik-ai/llmwiki"
    assert llmwiki.repositories.source.default_branch == "master"
    for project in ("falcone", "musematic", "llmwiki"):
        flows = catalog.flows(project)
        assert {flow.persona for flow in flows}.issuperset(
            {"anonymous", "standard-user", "privileged-admin"}
        )
        assert any(flow.mutation == "disposable" for flow in flows)
        for flow in flows:
            for step in flow.steps:
                if step.action == "request":
                    assert step.method is not None
                    assert step.expected_status is not None


@pytest.mark.asyncio
async def test_anonymous_catalog_api_flow_executes_real_http_contract(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "ok"}, headers={"x-request-id": "req-safe"})

    handler = BrowserActivityHandler(
        settings(tmp_path),
        api_transport=httpx.MockTransport(respond),
    )
    result = await handler.handle(
        "audit.api_flow",
        {
            "project": "falcone",
            "campaign_id": "campaign-test-1",
            "plan": {"flows": ["public-smoke"]},
        },
    )

    assert result["status"] == "PASS"
    assert seen == [("GET", "/healthz")]
    assert result["flows"][0]["requests"][0]["request_id"] == "req-safe"
    assert result["evidence"]["redaction_status"] == "REDACTED"


@pytest.mark.asyncio
async def test_authenticated_catalog_flow_fails_closed_without_reference(tmp_path: Path) -> None:
    handler = BrowserActivityHandler(settings(tmp_path))
    result = await handler.handle(
        "audit.api_flow",
        {
            "project": "musematic",
            "campaign_id": "campaign-test-2",
            "plan": {"flows": ["workspace-read"]},
        },
    )

    assert result["status"] == "BLOCKED_BY_CREDENTIAL"
    assert result["blocker"] == "PERSONA_CREDENTIAL_UNAVAILABLE"
    assert result["flows"] == []
    assert result["missing_credential_refs"] == ["MANUAL_MUSEMATIC_SUPERADMIN_INPUT_REQUIRED"]
