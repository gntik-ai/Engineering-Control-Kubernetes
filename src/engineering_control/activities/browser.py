from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.adapters.llmwiki import LLMWikiFalconeConsumerSuite
from engineering_control.config import Settings
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.engineering.models import E2EFlow, FlowStep
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.identity.attestation import superadmin_contract_hash
from engineering_control.identity.credentials import SuperadminCredentialLoader
from engineering_control.identity.persona_factory import EphemeralPersonaFactory
from engineering_control.identity.product_admin import HttpPersonaAdminClient
from engineering_control.identity.sessions import SuperadminSessionAdapter
from engineering_control.models.enums import Project, RedactionStatus
from engineering_control.security.redaction import SecretRedactor


class BrowserActivityHandler:
    """Run an allowlisted browser journey without placing credentials in workflow history."""

    def __init__(
        self,
        settings: Settings,
        runner: SafeCommandRunner | None = None,
        publisher: EvidencePublisher | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        consumer_runner: SafeCommandRunner | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner({"node": settings.node_executable})
        self.redactor = SecretRedactor()
        self.publisher = publisher or EvidencePublisher(settings, settings.browser_evidence_root)
        self.api_transport = api_transport
        self.consumer_suite = LLMWikiFalconeConsumerSuite(
            consumer_runner or SafeCommandRunner({"pnpm": settings.pnpm_executable})
        )

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step in {"identity.inspect_stale", "identity.recover_stale"}:
            return await self._stale_identity_operation(step, payload)
        if step == "identity.superadmin_attest":
            return await self._superadmin_attestation(payload)
        if step == "audit.api_flow":
            return await self._handle_api_catalog(payload)
        if step == "audit.browser_flow":
            return await self._handle_browser_catalog(payload)
        if step == "audit.llmwiki_consumer_suite":
            return await self._handle_llmwiki_consumer_suite(payload)
        if step != "runtime.browser_checks":
            raise ApplicationError("unsupported browser activity", non_retryable=True)
        plan = self._bounded_file(payload.get("journey_plan_path"), self.settings.journey_plan_root)
        self._validate_plan(plan)
        output = self._bounded_output(payload.get("output_directory"))
        self._clear_previous_attempt(output)
        if payload.get("credential_file") is not None:
            raise ApplicationError(
                "persistent browser persona credentials are prohibited",
                type="PolicyViolation",
                non_retryable=True,
            )
        credentials: dict[str, str] = {}
        environment = {
            "ENGINEERING_JOURNEY_PLAN": str(plan),
            "ENGINEERING_JOURNEY_OUTPUT": str(output),
            **credentials,
        }
        result = self.runner.run(
            "node",
            [str(self.settings.browser_script)],
            cwd=self.settings.browser_script.parent,
            timeout_seconds=3600,
            environment=environment,
            check=False,
        )
        result_path = output / "result.json"
        if not result_path.is_file():
            raise ApplicationError(
                f"browser runner failed without a result ({result.return_code})",
                type="BrowserRunnerFailure",
            )
        report = json.loads(result_path.read_text(encoding="utf-8"))
        safe = self._redact_known(self.redactor.redact(report).value, set(credentials.values()))
        self._atomic_json(result_path, safe)
        artifacts = []
        for artifact in sorted(output.iterdir()):
            if not artifact.is_file():
                continue
            os.chmod(artifact, 0o600)
            artifacts.append(
                {
                    "name": artifact.name,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "size_bytes": artifact.stat().st_size,
                    "redaction_status": (
                        "REDACTED" if artifact == result_path else "REVIEW_REQUIRED"
                    ),
                }
            )
        receipts = self._publish_result(payload, result_path)
        return {
            **payload,
            "status": safe.get("status", "FAILED") if isinstance(safe, dict) else "FAILED",
            "result": safe,
            "artifacts": artifacts,
            "evidence_receipts": receipts,
        }

    async def _stale_identity_operation(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "identity.recover_stale" and payload.get("dry_run") is not False:
            raise ApplicationError(
                "stale identity recovery requires explicit execution",
                type="PolicyViolation",
                non_retryable=True,
            )
        supplied = payload.get("stale_identity_candidates", {})
        if not isinstance(supplied, dict):
            raise ApplicationError("stale identity candidates are invalid", non_retryable=True)
        discovered: dict[str, list[dict[str, str]]] = {}
        recovered: dict[str, list[str]] = {}
        blockers: dict[str, str] = {}
        loader = SuperadminCredentialLoader(
            self.settings.browser_credential_root,
            production_permissions=self.settings.environment == "production",
        )
        for project in ("falcone", "musematic"):
            try:
                credential = loader.load(project)
                if not self._superadmin_target_valid(project, credential.base_url):
                    raise ApplicationError(
                        "superadmin target does not match the configured staging API",
                        type="SUPERADMIN_TARGET_INVALID",
                        non_retryable=True,
                    )
                adapter = SuperadminSessionAdapter(
                    redactor=self.redactor, transport=self.api_transport
                )
                async with adapter.login(credential) as session:
                    admin = HttpPersonaAdminClient(
                        session,
                        tenant_id=credential.tenant_id,
                        workspace_id=credential.workspace_id,
                        redactor=self.redactor,
                    )
                    try:
                        if step == "identity.inspect_stale":
                            discovered[project] = await admin.list_stale_managed_resources()
                        else:
                            project_candidates = supplied.get(project, [])
                            if not isinstance(project_candidates, list) or not all(
                                isinstance(item, dict) for item in project_candidates
                            ):
                                raise ApplicationError(
                                    "stale identity candidate set changed",
                                    type="PolicyViolation",
                                    non_retryable=True,
                                )
                            recovered[project] = await admin.recover_stale_managed_resources(
                                project_candidates
                            )
                    finally:
                        admin.destroy_ephemeral_secrets()
            except ApplicationError as error:
                blockers[project] = error.type or "SUPERADMIN_UNAVAILABLE"
        return {
            **payload,
            "status": "BLOCKED" if blockers else "PASS",
            "stale_identity_candidates": discovered
            if step == "identity.inspect_stale"
            else supplied,
            "recovered_managed_identities": recovered,
            "identity_recovery_blockers": blockers,
        }

    async def _superadmin_attestation(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = payload.get("project")
        if project not in {"falcone", "musematic"}:
            raise ApplicationError("superadmin attestation project is invalid", non_retryable=True)
        checked = datetime.now(UTC).isoformat()
        contract_hash = superadmin_contract_hash(str(project))
        loader = SuperadminCredentialLoader(
            self.settings.browser_credential_root,
            production_permissions=self.settings.environment == "production",
        )
        try:
            credential = loader.load(project)
            target_valid = self._superadmin_target_valid(project, credential.base_url)
            if not target_valid:
                raise ApplicationError(
                    "superadmin target does not match the configured staging API",
                    type="SUPERADMIN_TARGET_INVALID",
                    non_retryable=True,
                )
            adapter = SuperadminSessionAdapter(redactor=self.redactor, transport=self.api_transport)
            async with adapter.login(credential) as session:
                authentication_valid = True
                role_valid = session.superadmin_role_valid
            blocker = (
                "NONE"
                if target_valid and authentication_valid and role_valid
                else "SUPERADMIN_PREFLIGHT_FAILED"
            )
            return {
                "project": project,
                "contract_id": f"{project}-superadmin",
                "file_present": True,
                "parser_valid": True,
                "target_valid": target_valid,
                "authentication_valid": authentication_valid,
                "superadmin_role_valid": role_valid,
                "worker_identity": f"browser-runner@{os.uname().nodename}",
                "checked_timestamp": checked,
                "attestation_ttl_seconds": 300,
                "contract_hash": contract_hash,
                "blocker_code": blocker,
            }
        except ApplicationError as error:
            return {
                "project": project,
                "contract_id": f"{project}-superadmin",
                "file_present": error.type
                not in {f"MANUAL_{project.upper()}_SUPERADMIN_INPUT_REQUIRED"},
                "parser_valid": False,
                "target_valid": False,
                "authentication_valid": False,
                "superadmin_role_valid": False,
                "worker_identity": f"browser-runner@{os.uname().nodename}",
                "checked_timestamp": checked,
                "attestation_ttl_seconds": 300,
                "contract_hash": contract_hash,
                "blocker_code": error.type or "SUPERADMIN_PREFLIGHT_FAILED",
            }

    def _catalog(self) -> EngineeringCatalog:
        return EngineeringCatalog(
            project_root=self.settings.project_config_root,
            persona_root=self.settings.persona_config_root,
            flow_root=self.settings.flow_config_root,
            credential_root=self.settings.credential_root,
            attestation_root=self.settings.credential_attestation_root,
            browser_worker_identity=self.settings.browser_worker_identity,
        )

    def _selected_flows(self, payload: dict[str, Any]) -> tuple[EngineeringCatalog, list[E2EFlow]]:
        project = payload.get("project")
        if project not in {"falcone", "musematic", "llmwiki"}:
            raise ApplicationError("unknown E2E project", non_retryable=True)
        catalog = self._catalog()
        selected = payload.get("plan", {}).get("flows", payload.get("flows", []))
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise ApplicationError("E2E flow selection is invalid", non_retryable=True)
        selected_ids = set(selected)
        flows = [flow for flow in catalog.flows(project) if flow.id in selected_ids]
        if {flow.id for flow in flows} != selected_ids:
            raise ApplicationError(
                "E2E flow selection references an unknown flow", non_retryable=True
            )
        return catalog, flows

    @asynccontextmanager
    async def _persona_execution(
        self,
        catalog: EngineeringCatalog,
        project: str,
        persona_id: str,
        campaign_id: str,
        application_data_mutation_approved: bool = False,
    ) -> AsyncIterator[tuple[dict[str, str], Path | None]]:
        personas = {item.id: item for item in catalog.personas(project).personas}
        persona = personas.get(persona_id)
        if persona is None:
            raise ApplicationError("flow persona is absent from the catalog", non_retryable=True)
        if persona_id == "anonymous":
            yield {}, None
            return
        manifest = catalog.personas(project)
        authority = manifest.identity_authority
        loader = SuperadminCredentialLoader(
            self.settings.browser_credential_root,
            production_permissions=self.settings.environment == "production",
        )
        credential = loader.load(authority)
        if not self._superadmin_target_valid(authority, credential.base_url):
            raise ApplicationError(
                "superadmin target does not match the configured staging API",
                type="SUPERADMIN_TARGET_INVALID",
                non_retryable=True,
            )
        adapter = SuperadminSessionAdapter(redactor=self.redactor, transport=self.api_transport)
        async with adapter.login(credential) as session:
            admin = HttpPersonaAdminClient(
                session,
                tenant_id=credential.tenant_id,
                workspace_id=credential.workspace_id,
                redactor=self.redactor,
            )
            factory = EphemeralPersonaFactory(admin)
            try:
                async with factory.persona(
                    project=project,
                    campaign_id=campaign_id,
                    persona=persona_id,
                    scope=persona.scope,
                ) as generated:
                    environment = {
                        "JOURNEY_USERNAME": generated.username,
                        "JOURNEY_PASSWORD": generated.password,
                        "JOURNEY_SAFE_E2E_MUTATION": (
                            "true"
                            if self.settings.application_data_mutation_enabled
                            and application_data_mutation_approved
                            else "false"
                        ),
                    }
                    if generated.access_token:
                        environment["JOURNEY_ACCESS_TOKEN"] = generated.access_token
                    if credential.tenant_id:
                        environment["JOURNEY_TENANT_ID"] = credential.tenant_id
                    if credential.workspace_id:
                        environment["JOURNEY_WORKSPACE_ID"] = credential.workspace_id
                    for secret in environment.values():
                        self.redactor.register_secret(secret)
                        self.runner.redactor.register_secret(secret)
                    try:
                        yield (
                            environment,
                            self.settings.browser_credential_root / authority / "superadmin.env",
                        )
                    finally:
                        for secret in environment.values():
                            self.redactor.unregister_secret(secret)
                            self.runner.redactor.unregister_secret(secret)
            finally:
                admin.destroy_ephemeral_secrets()

    def _superadmin_target_valid(self, project: str, base_url: str) -> bool:
        configured = str(self._catalog().project(project).api_url).rstrip("/")
        return base_url.rstrip("/") == configured and "prod" not in base_url.lower()

    async def _handle_llmwiki_consumer_suite(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("project") != "falcone" or payload.get("preset") != "falcone-intensive":
            raise ApplicationError(
                "LLMWiki consumer suite is bound to falcone-intensive",
                type="PolicyViolation",
                non_retryable=True,
            )
        snapshot = payload.get("llmwiki_consumer_snapshot")
        if not isinstance(snapshot, dict):
            raise ApplicationError("LLMWiki consumer snapshot is missing", non_retryable=True)
        worktree = Path(str(snapshot.get("source_worktree", "")))
        catalog = self._catalog()
        campaign_id = self._bounded_campaign_id(payload.get("campaign_id"))
        async with self._persona_execution(
            catalog, "falcone", "developer-operator", campaign_id
        ) as (credentials, _):
            token = credentials.get("JOURNEY_ACCESS_TOKEN")
            if not token:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "EPHEMERAL_FALCONE_SESSION_TOKEN_UNAVAILABLE",
                }
            results = self.consumer_suite.run(
                source_worktree=worktree,
                command_ids=("lint", "test", "smoke", "e2e", "s14", "s14-integration"),
                falcone_access_token=token,
                falcone_api_base_url=str(catalog.project("falcone").api_url),
                campaign_id=campaign_id,
            )
        return {
            **payload,
            "status": "PASS" if all(item.status == "PASS" for item in results) else "FAILED",
            "llmwiki_consumer_results": [
                {
                    "command": item.command,
                    "status": item.status,
                    "return_code": item.return_code,
                }
                for item in results
            ],
            "llmwiki_consumer_cleanup": "PROVEN",
        }

    async def _handle_api_catalog(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog, flows = self._selected_flows(payload)
        project = str(payload["project"])
        manifest = catalog.project(project)
        campaign_id = self._bounded_campaign_id(payload.get("campaign_id"))
        summaries: list[dict[str, Any]] = []
        credential_blockers: list[str] = []
        status = "PASS"
        blocker: str | None = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            verify=True,
            transport=self.api_transport,
        ) as client:
            for flow in flows:
                steps = [item for item in flow.steps if item.channel == "api"]
                if not steps:
                    continue
                try:
                    async with self._persona_execution(
                        catalog,
                        project,
                        flow.persona,
                        campaign_id,
                        application_data_mutation_approved=(
                            payload.get("preset") == "full-e2e"
                            and payload.get("application_data_mutation_approved") is True
                        ),
                    ) as (credentials, _):
                        if (
                            flow.mutation == "disposable"
                            and credentials.get("JOURNEY_SAFE_E2E_MUTATION", "").lower() != "true"
                        ):
                            status = "BLOCKED"
                            blocker = "APPLICATION_DATA_MUTATION_NOT_AUTHORIZED"
                            break
                        result = await self._execute_api_flow(
                            client,
                            flow,
                            steps,
                            str(manifest.api_url),
                            campaign_id,
                            credentials,
                        )
                except ApplicationError as error:
                    credential_blockers.append(error.type or "SUPERADMIN_UNAVAILABLE")
                    continue
                summaries.append(result)
                if result["status"] != "PASS":
                    status = "FAILED"
                    blocker = "API_FLOW_FAILED"
                    break
        if credential_blockers:
            status = "BLOCKED_BY_CREDENTIAL"
            blocker = "PERSONA_CREDENTIAL_UNAVAILABLE"
        report = {
            "project": project,
            "campaign_id": campaign_id,
            "status": status,
            "blocker": blocker,
            "missing_credential_refs": sorted(set(credential_blockers)),
            "flows": summaries,
        }
        receipt = self._write_catalog_result("api", campaign_id, report)
        return {**payload, **report, "evidence": receipt}

    async def _execute_api_flow(
        self,
        client: httpx.AsyncClient,
        flow: E2EFlow,
        steps: list[FlowStep],
        api_url: str,
        campaign_id: str,
        credentials: dict[str, str],
    ) -> dict[str, Any]:
        created_id: str | None = None
        cleanup_attempted = False
        requests: list[dict[str, Any]] = []
        replacements = self._replacement_values(campaign_id, credentials)
        delete_step: FlowStep | None = None
        for candidate in steps:
            if candidate.method == "DELETE" and "<created-id>" in candidate.target:
                delete_step = candidate
        try:
            for step in steps:
                if created_id is not None:
                    replacements["created-id"] = created_id
                path = self._replace_template(step.target, replacements)
                if "<" in path or ">" in path:
                    return self._api_flow_failure(flow, requests, "REQUIRED_REFERENCE_MISSING")
                headers = {"Accept": "application/json", "X-E2E-Campaign-ID": campaign_id}
                token = credentials.get("JOURNEY_ACCESS_TOKEN")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                body = self._replace_body(step.body_template, replacements)
                response = await client.request(
                    str(step.method),
                    urljoin(api_url.rstrip("/") + "/", path.lstrip("/")),
                    headers=headers,
                    json=body,
                )
                request_summary = {
                    "method": step.method,
                    "path": path,
                    "expected_status": step.expected_status,
                    "actual_status": response.status_code,
                    "request_id": self._response_request_id(response),
                }
                requests.append(request_summary)
                if response.status_code != step.expected_status:
                    return self._api_flow_failure(flow, requests, "HTTP_STATUS_MISMATCH")
                if created_id is None and step.method == "POST" and response.content:
                    created_id = self._created_identifier(response)
                if step.method == "DELETE" and "<created-id>" in step.target:
                    cleanup_attempted = True
                if step.value_ref == "assert-json-absent:<created-id>" and created_id:
                    if created_id in response.text:
                        return self._api_flow_failure(flow, requests, "CLEANUP_NOT_VERIFIED")
            cleanup_verified = flow.mutation != "disposable" or (
                cleanup_attempted
                and bool(requests)
                and (
                    requests[-1]["actual_status"] == 404
                    or steps[-1].value_ref == "assert-json-absent:<created-id>"
                )
            )
            if not cleanup_verified:
                return self._api_flow_failure(flow, requests, "CLEANUP_NOT_VERIFIED")
            return {
                "flow_id": flow.id,
                "persona": flow.persona,
                "status": "PASS",
                "requests": requests,
                "created_resource_handle": self._opaque_handle(created_id),
                "cleanup_verified": cleanup_verified,
            }
        finally:
            if (
                flow.mutation == "disposable"
                and created_id
                and not cleanup_attempted
                and delete_step
            ):
                replacements["created-id"] = created_id
                cleanup_path = self._replace_template(delete_step.target, replacements)
                headers = {"X-E2E-Campaign-ID": campaign_id}
                if credentials.get("JOURNEY_ACCESS_TOKEN"):
                    headers["Authorization"] = f"Bearer {credentials['JOURNEY_ACCESS_TOKEN']}"
                try:
                    await client.request("DELETE", urljoin(api_url, cleanup_path), headers=headers)
                except httpx.HTTPError:
                    pass

    async def _handle_browser_catalog(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog, flows = self._selected_flows(payload)
        project = str(payload["project"])
        manifest = catalog.project(project)
        campaign_id = self._bounded_campaign_id(payload.get("campaign_id"))
        grouped: dict[str, list[E2EFlow]] = {}
        for flow in flows:
            if any(step.channel == "browser" for step in flow.steps):
                grouped.setdefault(str(flow.persona), []).append(flow)
        reports: list[dict[str, Any]] = []
        blockers: list[str] = []
        for persona, persona_flows in sorted(grouped.items()):
            ordered = sorted(persona_flows, key=self._browser_flow_order)
            try:
                async with self._persona_execution(
                    catalog,
                    project,
                    persona,
                    campaign_id,
                    application_data_mutation_approved=(
                        payload.get("preset") == "full-e2e"
                        and payload.get("application_data_mutation_approved") is True
                    ),
                ) as (credentials, credential_path):
                    plan_value = self._browser_plan(
                        ordered,
                        str(manifest.deployment.runtime_url),
                        str(manifest.api_url),
                        campaign_id,
                        credentials,
                    )
                    plan_root = self.settings.temporary_root / "browser-plans" / campaign_id
                    plan_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    plan_path = plan_root / f"{persona}.json"
                    self._atomic_json(plan_path, plan_value)
                    self._validate_plan(plan_path)
                    output = self.settings.browser_evidence_root / campaign_id / persona
                    self._clear_previous_attempt(output)
                    output.mkdir(parents=True, exist_ok=True, mode=0o700)
                    environment = {
                        "ENGINEERING_JOURNEY_PLAN": str(plan_path),
                        "ENGINEERING_JOURNEY_OUTPUT": str(output),
                        **credentials,
                    }
                    result = self.runner.run(
                        "node",
                        [str(self.settings.browser_script)],
                        cwd=self.settings.browser_script.parent,
                        timeout_seconds=3600,
                        environment=environment,
                        check=False,
                    )
            except (ApplicationError, KeyError) as error:
                blockers.append(f"{persona}:{getattr(error, 'type', None) or type(error).__name__}")
                continue
            result_path = output / "result.json"
            if not result_path.is_file():
                reports.append(
                    {"persona": persona, "status": "FAILED", "blocker": "BROWSER_RUNNER_FAILED"}
                )
                continue
            report = json.loads(result_path.read_text(encoding="utf-8"))
            safe = self._redact_known(self.redactor.redact(report).value, set(credentials.values()))
            self._atomic_json(result_path, safe)
            reports.append(
                {
                    "persona": persona,
                    "status": safe.get("status", "FAILED"),
                    "return_code": result.return_code,
                    "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                    "visual_evidence": safe.get("visual_evidence", "UNKNOWN"),
                }
            )
            if (
                credential_path is not None
                and safe.get("visual_evidence") != "DISABLED_CREDENTIAL_SAFETY"
            ):
                raise ApplicationError(
                    "credentialed journey produced visual evidence",
                    type="SecurityBlocker",
                    non_retryable=True,
                )
        status = (
            "BLOCKED_BY_CREDENTIAL"
            if blockers
            else "PASS"
            if all(item["status"] == "PASS" for item in reports)
            else "FAILED"
        )
        return {
            **payload,
            "status": status,
            "blocker": "PERSONA_CREDENTIAL_UNAVAILABLE" if blockers else None,
            "missing_credential_refs": sorted(set(blockers)),
            "browser_flows": reports,
        }

    @staticmethod
    def _browser_flow_order(flow: E2EFlow) -> tuple[int, str]:
        if flow.id.startswith("valid-login"):
            return (0, flow.id)
        if flow.id.startswith("session-refresh"):
            return (10, flow.id)
        if flow.id.startswith("logout"):
            return (100, flow.id)
        if flow.category == "authorization":
            return (70, flow.id)
        return (30, flow.id)

    def _browser_plan(
        self,
        flows: list[E2EFlow],
        runtime_url: str,
        api_url: str,
        campaign_id: str,
        credentials: dict[str, str],
    ) -> dict[str, Any]:
        replacements = self._replacement_values(campaign_id, credentials)
        plan_steps: list[dict[str, Any]] = []
        credential_names = {
            "credential.username": "JOURNEY_USERNAME",
            "credential.password": "JOURNEY_PASSWORD",
        }
        for flow in flows:
            for step in flow.steps:
                if step.channel != "browser":
                    continue
                if step.action == "navigate":
                    target = self._replace_template(step.target, replacements)
                    if "<" in target or ">" in target:
                        raise KeyError("JOURNEY_ROUTE_REFERENCE")
                    plan_steps.append({"action": "goto", "url": urljoin(runtime_url, target)})
                elif step.action in {"fill-credential", "fill-generated"}:
                    generated_names = {
                        "ephemeral.username": "JOURNEY_USERNAME",
                        "ephemeral.password": "JOURNEY_PASSWORD",
                    }
                    environment_name = credential_names.get(
                        str(step.value_ref)
                    ) or generated_names.get(str(step.value_ref))
                    if environment_name is None or not credentials.get(environment_name):
                        raise KeyError(environment_name or "JOURNEY_CREDENTIAL_FIELD")
                    plan_steps.append(
                        {
                            "action": "fill-selector",
                            "selector": step.target,
                            "value_from_env": environment_name,
                        }
                    )
                elif step.action == "click":
                    plan_steps.append({"action": "click-selector", "selector": step.target})
                elif step.action == "assert-visible":
                    if step.value_ref:
                        plan_steps.append(
                            {"action": "wait-for-text", "text": step.value_ref, "exact": True}
                        )
                    else:
                        plan_steps.append({"action": "expect-selector", "selector": step.target})
                elif step.action == "assert-url":
                    plan_steps.append({"action": "expect-url", "path_contains": step.target})
                elif step.action == "capture":
                    name = re.sub(r"[^a-z0-9-]", "-", step.target.lower()).strip("-")
                    plan_steps.append({"action": "capture", "name": name or "capture"})
                elif step.action == "assert-status":
                    raise ApplicationError(
                        f"browser flow action {step.action} has no safe runner mapping",
                        non_retryable=True,
                    )
        runtime_origin = self._origin(runtime_url)
        api_origin = self._origin(api_url)
        return {
            "start_url": runtime_url,
            "allowed_origins": sorted({runtime_origin, api_origin}),
            "browser": "chromium",
            "timeout_ms": min(
                max(sum(flow.timeout_seconds for flow in flows) * 1000, 1000), 120000
            ),
            "capture_visual_evidence": not bool(credentials),
            "steps": plan_steps,
        }

    @staticmethod
    def _origin(value: str) -> str:
        parsed = urlparse(value)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _replacement_values(campaign_id: str, credentials: dict[str, str]) -> dict[str, str]:
        mapping = {
            "campaign-id": campaign_id,
            "tenant-id": credentials.get("JOURNEY_TENANT_ID", ""),
            "other-tenant-id": credentials.get("JOURNEY_OTHER_TENANT_ID", ""),
            "configured-tenant-id": credentials.get("JOURNEY_TENANT_ID", ""),
            "workspace-id": credentials.get("JOURNEY_WORKSPACE_ID", ""),
            "session-id": credentials.get("JOURNEY_SESSION_ID", ""),
            "credential.refresh-token": credentials.get("JOURNEY_REFRESH_TOKEN", ""),
        }
        return {key: value for key, value in mapping.items() if value}

    @staticmethod
    def _replace_template(value: str, replacements: dict[str, str]) -> str:
        result = value
        for key, replacement in replacements.items():
            result = result.replace(f"<{key}>", quote(replacement, safe=""))
        return result

    @classmethod
    def _replace_body(
        cls, value: dict[str, object] | None, replacements: dict[str, str]
    ) -> dict[str, object] | None:
        if value is None:
            return None

        def replace(item: object) -> object:
            if isinstance(item, str):
                result = item
                for key, replacement in replacements.items():
                    result = result.replace(f"<{key}>", replacement)
                return result
            if isinstance(item, dict):
                return {str(key): replace(nested) for key, nested in item.items()}
            if isinstance(item, list):
                return [replace(nested) for nested in item]
            return item

        return {key: replace(item) for key, item in value.items()}

    @staticmethod
    def _response_request_id(response: httpx.Response) -> str | None:
        for name in ("x-request-id", "x-correlation-id", "traceparent"):
            value = response.headers.get(name)
            if value:
                return str(value)[:200]
        return None

    @staticmethod
    def _created_identifier(response: httpx.Response) -> str | None:
        try:
            value = response.json()
        except ValueError:
            return None

        def find(item: Any) -> str | None:
            if isinstance(item, dict):
                for key in ("workspaceId", "workspace_id", "noteId", "note_id", "id"):
                    candidate = item.get(key)
                    if isinstance(candidate, str) and 0 < len(candidate) <= 200:
                        return candidate
                for nested in item.values():
                    candidate = find(nested)
                    if candidate:
                        return candidate
            return None

        return find(value)

    @staticmethod
    def _api_flow_failure(
        flow: E2EFlow, requests: list[dict[str, Any]], blocker: str
    ) -> dict[str, Any]:
        return {
            "flow_id": flow.id,
            "persona": flow.persona,
            "status": "FAILED",
            "blocker": blocker,
            "requests": requests,
            "cleanup_verified": False if flow.mutation == "disposable" else None,
        }

    @staticmethod
    def _opaque_handle(value: str | None) -> str | None:
        if value is None:
            return None
        return "resource-sha256:" + hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _bounded_campaign_id(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,120}", value):
            raise ApplicationError("campaign ID is invalid", non_retryable=True)
        return value

    def _write_catalog_result(
        self, channel: str, campaign_id: str, report: dict[str, Any]
    ) -> dict[str, Any]:
        output = self.settings.browser_evidence_root / campaign_id / channel
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = output / "result.json"
        self._atomic_json(path, report)
        return {
            "handle": f"browser-evidence:{campaign_id}:{channel}:result.json",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "redaction_status": "REDACTED",
        }

    def _publish_result(self, payload: dict[str, Any], result_path: Path) -> list[dict[str, Any]]:
        orchestration = payload.get("orchestration")
        project = payload.get("project")
        change_set_id = payload.get("change_set_id")
        if (
            not isinstance(orchestration, dict)
            or not isinstance(orchestration.get("workflow_id"), str)
            or not isinstance(project, str)
            or not isinstance(change_set_id, str)
        ):
            if self.settings.environment == "production":
                raise ApplicationError(
                    "production browser evidence requires authoritative workflow metadata",
                    type="SecurityBlocker",
                    non_retryable=True,
                )
            return []
        receipt = self.publisher.publish(
            result_path,
            project=Project(project),
            workflow_id=str(orchestration["workflow_id"]),
            change_set_id=UUID(change_set_id),
            category="changes",
            filename=f"runtime-browser-{payload['deployment_id']}.json",
            producer="browser-runner",
            content_type="application/json",
            redaction_status=RedactionStatus.REDACTED,
        )
        return [receipt]

    def _validate_plan(self, path: Path) -> None:
        try:
            schema = json.loads(self.settings.journey_schema.read_text(encoding="utf-8"))
            plan = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(plan)
        except (OSError, json.JSONDecodeError, SchemaError, ValidationError) as error:
            raise ApplicationError(
                "browser journey failed strict schema validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error

    @staticmethod
    def _bounded_file(value: Any, root: Path) -> Path:
        if not isinstance(value, str):
            raise ApplicationError("browser plan path is required", non_retryable=True)
        resolved_root = root.resolve()
        path = Path(value).resolve()
        if not path.is_file() or not path.is_relative_to(resolved_root):
            raise ApplicationError(
                "browser plan is outside its configured root",
                type="PolicyViolation",
                non_retryable=True,
            )
        return path

    def _bounded_output(self, value: Any) -> Path:
        if not isinstance(value, str):
            raise ApplicationError("browser output directory is required", non_retryable=True)
        root = self.settings.browser_evidence_root.resolve()
        path = Path(value).resolve()
        if path == root or not path.is_relative_to(root):
            raise ApplicationError(
                "browser output is outside its evidence root",
                type="PolicyViolation",
                non_retryable=True,
            )
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        return path

    @staticmethod
    def _clear_previous_attempt(output: Path) -> None:
        for filename in ("result.json", "trace.zip", "final.png", "failure.png"):
            candidate = output / filename
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()

    @classmethod
    def _redact_known(cls, value: Any, secrets: set[str]) -> Any:
        if isinstance(value, str):
            result = value
            for secret in sorted((item for item in secrets if item), key=len, reverse=True):
                result = result.replace(secret, "[REDACTED:journey-credential]")
            return result
        if isinstance(value, dict):
            return {key: cls._redact_known(item, secrets) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_known(item, secrets) for item in value]
        return value

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        content = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
