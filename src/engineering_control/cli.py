from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from engineering_control.security.redaction import SecretRedactor


def _bounded_integer(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return parsed

    return parse


class APIClient:
    def __init__(self, base_url: str, token_file: Path) -> None:
        try:
            operator = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError as error:
            raise PermissionError("engineering-ctl must run as the hermes operator") from error
        if operator != "hermes":
            raise PermissionError("engineering-ctl must run as the hermes operator")
        metadata = token_file.stat()
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO):
            raise PermissionError("API token file permissions are too broad")
        token = token_file.read_text(encoding="utf-8").strip()
        self.client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Engineering-Actor": f"engineering-ctl:{operator}",
            },
            timeout=30,
        )

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = (
            {"Idempotency-Key": f"engineering-ctl-{uuid4()}"} if path == "/v1/commands" else None
        )
        response = self.client.request(method, path, json=payload, headers=headers)
        response.raise_for_status()
        return SecretRedactor().redact(response.json()).value


_CHANGE_REPOSITORIES = ("falcone", "falcone-charts", "musematic", "musematic-deploy", "llmwiki")
_CHANGE_DESCRIPTION_MAX_BYTES = 8 * 1024


def _change_description(arguments: argparse.Namespace) -> str:
    if arguments.text is not None:
        return str(arguments.text)
    path = Path(arguments.file)
    if path.stat().st_size > _CHANGE_DESCRIPTION_MAX_BYTES:
        raise ValueError("change description file exceeds 8 KiB")
    return path.read_text(encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="engineering-ctl")
    root.add_argument(
        "--api-url",
        default=os.getenv("ENGINEERING_API_URL", "http://127.0.0.1:8740"),
    )
    root.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.getenv("ENGINEERING_API_TOKEN_FILE", "/etc/engineering-control/env/api.token")
        ),
    )
    commands = root.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit")
    audit.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    audit.add_argument("--name", required=True)
    audit.add_argument("--source-sha", required=True)
    audit.add_argument("--deployment-sha", required=True)
    audit.add_argument("--domains", nargs="+", required=True)
    audit.add_argument("--personas", nargs="+", required=True)
    audit.add_argument("--lenses", nargs="+", required=True)
    audit.add_argument("--environments", nargs="+", required=True)
    audit.add_argument("--execute", action="store_true")
    for action in ("plan", "start"):
        bounded_audit = commands.add_parser(f"audit-{action}")
        bounded_audit.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
        bounded_audit.add_argument(
            "preset",
            choices=(
                "snapshot",
                "static-full",
                "e2e-smoke",
                "full-e2e",
                "security",
                "ux",
                "deployment",
                "documentation",
                "falcone-intensive",
            ),
        )
        bounded_audit.add_argument("environment", choices=("staging",))
        if action == "start":
            bounded_audit.add_argument("--publish-findings", action="store_true")

    audit_all = commands.add_parser("audit-all")
    audit_all.add_argument("mode", choices=("plan", "start"))
    audit_all.add_argument(
        "preset",
        choices=(
            "snapshot",
            "static-full",
            "e2e-smoke",
            "full-e2e",
            "security",
            "ux",
            "deployment",
            "documentation",
        ),
    )
    audit_all.add_argument("environment", choices=("staging",))
    audit_all.add_argument("--publish-findings", action="store_true")

    commands.add_parser("campaigns").add_argument("--limit", type=int, default=50)
    campaign = commands.add_parser("campaign")
    campaign.add_argument("campaign_id")
    ready = commands.add_parser("ready")
    ready.add_argument("scope", choices=("core", "all"))
    connections = commands.add_parser("connections")
    connections.add_argument("scope", choices=("all",))
    temporal = commands.add_parser("temporal")
    temporal.add_argument("view", choices=("summary",))
    workers = commands.add_parser("workers")
    workers.add_argument("scope", choices=("all",))
    commands.add_parser("providers")
    commands.add_parser("channel-check")
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("workflow_id")
    tail = commands.add_parser("tail")
    tail.add_argument("workflow_id")
    tail.add_argument("limit", nargs="?", type=int, default=50, choices=range(1, 101))
    nudge = commands.add_parser("nudge")
    nudge.add_argument("workflow_id")
    commands.add_parser("recheck-connections")
    campaign_reconcile = commands.add_parser("campaign-reconcile")
    campaign_reconcile.add_argument(
        "--limit", type=_bounded_integer(1, 200), default=100, metavar="1-200"
    )
    campaign_reconcile.add_argument(
        "--offset", type=_bounded_integer(0, 10_000), default=0, metavar="0-10000"
    )
    for action in ("locate", "start"):
        issue = commands.add_parser(f"issue-{action}")
        issue.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
        issue.add_argument("surface", choices=("source", "deployment"))
        issue.add_argument("issue_number", type=int)
        if action == "start":
            issue.add_argument(
                "--delivery-mode",
                choices=("source-pr", "full-delivery"),
                default="source-pr",
            )
            issue.add_argument(
                "--runtime-proof",
                choices=("runtime", "source-revalidation"),
                default="runtime",
            )
            # NEW-95: start an R3 issue with the binding of its approved R3 candidate.
            issue.add_argument("--r3-approval", metavar="BINDING", default=None)
    r3_candidate = commands.add_parser("r3-candidate")
    r3_candidate.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    r3_candidate.add_argument("surface", choices=("source", "deployment"))
    r3_candidate.add_argument("issue_number", type=int)
    r3_approve = commands.add_parser("r3-approve")
    r3_approve.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    r3_approve.add_argument("surface", choices=("source", "deployment"))
    r3_approve.add_argument("issue_number", type=int)
    r3_approve.add_argument("binding")
    r3_approve.add_argument("confirmation_phrase")
    commands.add_parser("findings").add_argument("--limit", type=int, default=50)
    for name in ("status", "run"):
        commands.add_parser(name).add_argument("workflow_id")
    for name in ("pause", "resume", "cancel"):
        commands.add_parser(name).add_argument("workflow_id")
    commands.add_parser("usage")
    commands.add_parser("evidence").add_argument("artifact_id")
    commands.add_parser("export-campaign").add_argument("campaign_id")
    commands.add_parser("export-workflow").add_argument("workflow_id")
    commands.add_parser("operation-export").add_argument("operation_id")
    commands.add_parser("verify-finding").add_argument("finding_id")
    delivery = commands.add_parser("delivery")
    delivery.add_argument("finding_id")
    delivery.add_argument("--risk", choices=("R0", "R1", "R2", "R3"), required=True)
    delivery.add_argument("--execute", action="store_true")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("change_set_id")
    reconcile.add_argument("--candidate-build-run-id", type=int)
    reconcile.add_argument("--execute", action="store_true")
    deployment = commands.add_parser("deployment")
    deployment.add_argument("change_set_id")
    deployment.add_argument("--environment", choices=("preview", "staging"), required=True)
    deployment.add_argument("--execute", action="store_true")
    commands.add_parser("verify-runtime").add_argument("deployment_id")
    commands.add_parser("approval-candidate").add_argument("change_set_id")
    for decision in ("approve", "reject"):
        approval = commands.add_parser(decision)
        approval.add_argument("change_set_id")
        approval.add_argument("artifact_binding_sha256")
        approval.add_argument("--typed-confirmation")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("deployment_id")
    rollback.add_argument("artifact_binding_sha256")
    rollback.add_argument("--execute", action="store_true")
    validation = commands.add_parser("validate-lifecycle")
    validation.add_argument("--hold-seconds", type=int, default=5)
    bootstrap_plan = commands.add_parser("bootstrap-plan")
    bootstrap_plan.add_argument("project", choices=("llmwiki",))
    bootstrap_plan.add_argument("environment", choices=("staging",))
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("project", choices=("llmwiki",))
    bootstrap.add_argument("environment", choices=("staging",))
    bootstrap.add_argument("approval_binding")
    bootstrap.add_argument("--execute", action="store_true", required=True)
    backlog_import = commands.add_parser("backlog-import")
    backlog_import.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    backlog_import.add_argument(
        "import_filter",
        choices=("bugs", "bugs-and-docs", "security", "enhancements", "all"),
    )
    backlog_import.add_argument("--max-items", type=int, default=500, choices=range(1, 501))
    backlog_inventory = commands.add_parser("backlog-inventory")
    backlog_inventory.add_argument("campaign_id")
    backlog_inventory.add_argument("--limit", type=int, default=20, choices=range(1, 201))
    backlog_plan = commands.add_parser("backlog-plan")
    backlog_plan.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    backlog_plan.add_argument("campaign_id")
    backlog_candidate = commands.add_parser("backlog-approval-candidate")
    backlog_candidate.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    backlog_candidate.add_argument("campaign_id")
    backlog_candidate.add_argument("delivery_mode", choices=("source-pr", "full-delivery"))
    backlog_candidate.add_argument("maximum_risk", choices=("R0", "R1", "R2", "R3"))
    backlog_candidate.add_argument("--max-items", type=int, default=1, choices=range(1, 51))
    backlog_approve = commands.add_parser("backlog-approve")
    backlog_approve.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    backlog_approve.add_argument("campaign_id")
    backlog_approve.add_argument("approval_binding")
    backlog_approve.add_argument(
        "typed_confirmation", choices=("APPROVE_EXISTING_GITHUB_BACKLOG_LOOP",)
    )
    backlog_release_ownership = commands.add_parser("backlog-release-ownership")
    backlog_release_ownership.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    backlog_release_ownership.add_argument("campaign_id")
    backlog_release_ownership.add_argument(
        "typed_confirmation", choices=("RELEASE_GITHUB_WRITE_POLICY_OWNERSHIP",)
    )
    backlog_start = commands.add_parser("backlog-start")
    backlog_start.add_argument("project", choices=("falcone", "musematic", "llmwiki"))
    backlog_start.add_argument("campaign_id")
    backlog_start.add_argument("maximum_risk", choices=("R0", "R1", "R2", "R3"))
    backlog_start.add_argument("delivery_mode", choices=("source-pr", "full-delivery"))
    backlog_start.add_argument("--max-items", type=int, default=1, choices=range(1, 51))
    backlog_status = commands.add_parser("backlog-status")
    backlog_status.add_argument("workflow_id")
    global_backlog = commands.add_parser("global-backlog-start")
    global_backlog.add_argument("campaign_id")
    global_backlog.add_argument("policy_campaign_id")
    global_backlog.add_argument("falcone_campaign_id")
    global_backlog.add_argument("musematic_campaign_id")
    global_backlog.add_argument("llmwiki_campaign_id")
    global_backlog.add_argument("typed_confirmation", choices=("START_GLOBAL_STAGING_R2_BACKLOG",))
    for action in ("pause", "pause-after-current", "resume", "stop-after-current", "cancel"):
        control = commands.add_parser(f"backlog-{action}")
        control.add_argument("workflow_id")
    for action in ("skip", "retry"):
        issue_action = commands.add_parser(f"backlog-{action}")
        issue_action.add_argument("workflow_id")
        issue_action.add_argument(
            "repository",
            choices=(
                "falcone",
                "falcone-charts",
                "musematic",
                "musematic-deploy",
                "llmwiki",
            ),
        )
        issue_action.add_argument("issue_number", type=int)
        if action == "skip":
            issue_action.add_argument(
                "reason_code",
                choices=(
                    "DUPLICATE",
                    "ALREADY_FIXED",
                    "SUPERSEDED",
                    "NEEDS_PRODUCT_DECISION",
                    "BLOCKED_BY_DEPENDENCY",
                    "BLOCKED_BY_RUNTIME_BASELINE",
                    "R3_REQUIRES_OPERATOR",
                    "NOT_REPRODUCIBLE",
                    "ENHANCEMENT_DEFERRED",
                ),
            )
    # NEW-108 operator change requests: draft -> review -> approve -> publish one issue.
    change_draft = commands.add_parser("change-draft")
    change_draft.add_argument(
        "--project", choices=("falcone", "musematic", "llmwiki"), required=True
    )
    change_source = change_draft.add_mutually_exclusive_group(required=True)
    change_source.add_argument("--file", type=Path)
    change_source.add_argument("--text")
    change_draft.add_argument("--repository", choices=_CHANGE_REPOSITORIES)
    changes = commands.add_parser("changes")
    changes.add_argument("--project", choices=("falcone", "musematic", "llmwiki"))
    changes.add_argument(
        "--status",
        choices=("DRAFTING", "CANDIDATE", "APPROVED", "PUBLISHED", "REJECTED", "FAILED"),
    )
    changes.add_argument("--limit", type=_bounded_integer(1, 200), default=20, metavar="1-200")
    commands.add_parser("change-show").add_argument("change_request_id")
    change_approve = commands.add_parser("change-approve")
    change_approve.add_argument("change_request_id")
    change_approve.add_argument("approval_binding")
    change_approve.add_argument("typed_confirmation")
    change_revise = commands.add_parser("change-revise")
    change_revise.add_argument("change_request_id")
    change_revise.add_argument("--note", required=True)
    change_reject = commands.add_parser("change-reject")
    change_reject.add_argument("change_request_id")
    change_reject.add_argument("--reason", required=True)
    dashboard = commands.add_parser("dashboard")
    dashboard_commands = dashboard.add_subparsers(dest="dashboard_command", required=True)
    for name in ("init-password", "reset-password"):
        password = dashboard_commands.add_parser(name)
        password.add_argument("--output-file", type=Path, required=True)
        password.add_argument("--force-output", action="store_true")
    return root


def dispatch(client: APIClient, arguments: argparse.Namespace) -> Any:
    command = arguments.command
    if command == "audit":
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": "audit_campaign",
                "project": arguments.project,
                "name": arguments.name,
                "source_sha": arguments.source_sha,
                "deployment_sha": arguments.deployment_sha,
                "domains": arguments.domains,
                "personas": arguments.personas,
                "lenses": arguments.lenses,
                "environments": arguments.environments,
                "execute": arguments.execute,
                "production": False,
            },
        )
    if command in {"audit-plan", "audit-start"}:
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": command.replace("-", "_"),
                "project": arguments.project,
                "preset": arguments.preset,
                "environment": arguments.environment,
                "publish_findings": (
                    arguments.publish_findings if command == "audit-start" else False
                ),
                **({"production": False} if command == "audit-start" else {}),
            },
        )
    if command == "audit-all":
        if arguments.mode == "plan" and arguments.publish_findings:
            raise ValueError("audit-all planning cannot publish findings")
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": "audit_all_projects",
                "mode": arguments.mode,
                "preset": arguments.preset,
                "environment": arguments.environment,
                "publish_findings": arguments.publish_findings,
                "production": False,
            },
        )
    if command in {"campaigns", "findings"}:
        return client.request("GET", f"/v1/{command}?limit={arguments.limit}")
    if command == "campaign":
        return client.request("GET", f"/v1/campaign-detail/{arguments.campaign_id}")
    if command == "ready":
        return client.request("GET", f"/v1/readiness/{arguments.scope}")
    if command == "connections":
        return client.request("GET", "/v1/connections?scope=all")
    if command == "temporal":
        return client.request("GET", "/v1/temporal/summary")
    if command == "workers":
        return client.request("GET", "/v1/workers")
    if command == "providers":
        return client.request("GET", "/v1/providers")
    if command == "channel-check":
        return client.request("GET", "/v1/channel-check")
    if command == "diagnose":
        return client.request("GET", f"/v1/runs/{arguments.workflow_id}")
    if command == "tail":
        return client.request(
            "GET", f"/v1/workflows/{arguments.workflow_id}/tail?limit={arguments.limit}"
        )
    if command == "nudge":
        return client.request(
            "POST", "/v1/commands", {"action": "nudge", "workflow_id": arguments.workflow_id}
        )
    if command == "recheck-connections":
        return client.request(
            "POST", "/v1/commands", {"action": "refresh_connections", "scope": "all"}
        )
    if command == "campaign-reconcile":
        query = httpx.QueryParams({"limit": arguments.limit, "offset": arguments.offset})
        return client.request("POST", f"/v1/campaigns/reconcile?{query}", {})
    if command in {"issue-locate", "issue-start"}:
        query = httpx.QueryParams(
            {
                "project": arguments.project,
                "surface": arguments.surface,
                "issue_number": arguments.issue_number,
            }
        )
        located = client.request("GET", f"/v1/backlogs/locate?{query}")
        if command == "issue-locate":
            return located
        issue = located.get("issue") if isinstance(located, dict) else None
        if not isinstance(issue, dict):
            raise ValueError("located issue projection is invalid")
        solve: dict[str, Any] = {
            "action": "solve_issue",
            "project": arguments.project,
            "backlog_campaign_id": located["backlog_campaign_id"],
            "repository": issue["repository"],
            "issue_number": arguments.issue_number,
            "maximum_risk": "R2",
            "delivery_mode": arguments.delivery_mode,
            "correction_limit": 5,
            "production": False,
        }
        if arguments.runtime_proof != "runtime":
            # NEW-88: sent only when waived, so the default request is unchanged.
            solve["runtime_proof"] = arguments.runtime_proof
        if arguments.r3_approval is not None:
            # NEW-95: an approved R3 candidate lifts the bound to R3 for this issue only.
            solve["maximum_risk"] = "R3"
            solve["r3_approval_binding"] = arguments.r3_approval
        return client.request("POST", "/v1/commands", solve)
    if command == "r3-candidate":
        query = httpx.QueryParams(
            {
                "project": arguments.project,
                "surface": arguments.surface,
                "issue_number": arguments.issue_number,
            }
        )
        return client.request("POST", f"/v1/r3-issues/candidate?{query}")
    if command == "r3-approve":
        return client.request(
            "POST",
            "/v1/r3-issues/approve",
            {
                "project": arguments.project,
                "surface": arguments.surface,
                "issue_number": arguments.issue_number,
                "binding": arguments.binding,
                "confirmation_phrase": arguments.confirmation_phrase,
            },
        )
    if command in {"status", "run"}:
        return client.request("GET", f"/v1/runs/{arguments.workflow_id}")
    if command in {"pause", "resume", "cancel"}:
        return client.request(
            "POST", "/v1/commands", {"action": command, "workflow_id": arguments.workflow_id}
        )
    if command == "usage":
        return client.request("GET", "/v1/usage")
    if command == "evidence":
        return client.request("GET", f"/v1/evidence/{arguments.artifact_id}")
    if command == "export-campaign":
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": "evidence_export_campaign",
                "campaign_id": arguments.campaign_id,
                "format": "json",
            },
        )
    if command == "export-workflow":
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": "evidence_export_workflow",
                "workflow_id": arguments.workflow_id,
                "format": "json",
            },
        )
    if command == "operation-export":
        return client.request("GET", f"/v1/operations/{arguments.operation_id}/export")
    if command == "verify-finding":
        return client.request("POST", "/v1/findings/verify", {"finding_id": arguments.finding_id})
    if command == "delivery":
        return client.request(
            "POST",
            "/v1/deliveries",
            {
                "finding_id": arguments.finding_id,
                "risk_class": arguments.risk,
                "dry_run": not arguments.execute,
            },
        )
    if command == "reconcile":
        return client.request(
            "POST",
            "/v1/deliveries/reconcile",
            {
                "change_set_id": arguments.change_set_id,
                "candidate_build_run_id": arguments.candidate_build_run_id,
                "dry_run": not arguments.execute,
            },
        )
    if command == "deployment":
        return client.request(
            "POST",
            "/v1/deployments/prepare",
            {
                "change_set_id": arguments.change_set_id,
                "environment": arguments.environment,
                "dry_run": not arguments.execute,
            },
        )
    if command == "verify-runtime":
        return client.request(
            "POST",
            "/v1/deployments/verify-runtime",
            {"deployment_id": arguments.deployment_id},
        )
    if command == "approval-candidate":
        return client.request("GET", f"/v1/approvals/{arguments.change_set_id}/candidate")
    if command in {"approve", "reject"}:
        return client.request(
            "POST",
            "/v1/approvals",
            {
                "change_set_id": arguments.change_set_id,
                "artifact_binding_sha256": arguments.artifact_binding_sha256,
                "decision": command,
                "typed_confirmation": arguments.typed_confirmation,
            },
        )
    if command == "rollback":
        return client.request(
            "POST",
            "/v1/deployments/rollback",
            {
                "deployment_id": arguments.deployment_id,
                "expected_artifact_binding_sha256": arguments.artifact_binding_sha256,
                "dry_run": not arguments.execute,
            },
        )
    if command == "validate-lifecycle":
        return client.request(
            "POST",
            "/v1/validation/lifecycle",
            {"hold_seconds": arguments.hold_seconds},
        )
    if command == "bootstrap-plan":
        return client.request(
            "POST",
            "/v1/bootstrap-plan",
            {"project": arguments.project, "environment": arguments.environment},
        )
    if command == "bootstrap":
        return client.request(
            "POST",
            "/v1/bootstrap",
            {
                "project": arguments.project,
                "environment": arguments.environment,
                "approval_binding": arguments.approval_binding,
                "execute": True,
            },
        )
    if command == "backlog-import":
        return client.request(
            "POST",
            "/v1/backlogs/import",
            {
                "project": arguments.project,
                "source": "github-open",
                "import_filter": arguments.import_filter,
                "maximum_items": arguments.max_items,
            },
        )
    if command == "backlog-inventory":
        return client.request(
            "GET", f"/v1/backlogs/{arguments.campaign_id}/issues?limit={arguments.limit}"
        )
    if command == "backlog-plan":
        return client.request(
            "POST",
            "/v1/loop-plan",
            {"project": arguments.project, "campaign_id": arguments.campaign_id},
        )
    if command == "backlog-approval-candidate":
        return client.request(
            "POST",
            f"/v1/backlogs/{arguments.campaign_id}/approval-candidate",
            {
                "project": arguments.project,
                "delivery_mode": arguments.delivery_mode,
                "maximum_risk": arguments.maximum_risk,
                "maximum_items": arguments.max_items,
            },
        )
    if command == "backlog-approve":
        return client.request(
            "POST",
            f"/v1/backlogs/{arguments.campaign_id}/approve",
            {
                "project": arguments.project,
                "approval_binding": arguments.approval_binding,
                "typed_confirmation": arguments.typed_confirmation,
            },
        )
    if command == "backlog-release-ownership":
        return client.request(
            "POST",
            f"/v1/backlogs/{arguments.campaign_id}/release-ownership",
            {
                "project": arguments.project,
                "typed_confirmation": arguments.typed_confirmation,
            },
        )
    if command == "backlog-start":
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": "solve_backlog",
                "project": arguments.project,
                "backlog_campaign_id": arguments.campaign_id,
                "maximum_risk": arguments.maximum_risk,
                "delivery_mode": arguments.delivery_mode,
                "maximum_items": arguments.max_items,
            },
        )
    if command == "backlog-status":
        return client.request("GET", f"/v1/loops/{arguments.workflow_id}")
    if command == "global-backlog-start":
        return client.request(
            "POST",
            "/v1/commands",
            {
                "action": "start_autonomous_backlog",
                "campaign_id": arguments.campaign_id,
                "policy_campaign_id": arguments.policy_campaign_id,
                "backlog_campaign_ids": {
                    "falcone": arguments.falcone_campaign_id,
                    "musematic": arguments.musematic_campaign_id,
                    "llmwiki": arguments.llmwiki_campaign_id,
                },
                "environment": "staging",
                "maximum_risk": "R2",
                "global_active_issues": 1,
                "global_deployments": 1,
                "implementation_correction_limit": 5,
                "deployment_correction_limit": 3,
                "production": False,
                "typed_confirmation": arguments.typed_confirmation,
            },
        )
    if command in {
        "backlog-pause",
        "backlog-pause-after-current",
        "backlog-resume",
        "backlog-stop-after-current",
        "backlog-cancel",
    }:
        action = command.removeprefix("backlog-").replace("-", "_")
        return client.request(
            "POST", "/v1/commands", {"action": action, "workflow_id": arguments.workflow_id}
        )
    if command in {"backlog-skip", "backlog-retry"}:
        action = command.removeprefix("backlog-")
        payload = {
            "action": action,
            "workflow_id": arguments.workflow_id,
            "repository": arguments.repository,
            "issue_number": arguments.issue_number,
        }
        if action == "skip":
            payload["reason_code"] = arguments.reason_code
        return client.request("POST", "/v1/commands", payload)
    if command == "change-draft":
        draft: dict[str, Any] = {
            "project": arguments.project,
            "description": _change_description(arguments),
        }
        if arguments.repository is not None:
            draft["target_repository"] = f"gntik-ai/{arguments.repository}"
        return client.request("POST", "/v1/change-requests", draft)
    if command == "changes":
        query = httpx.QueryParams(
            {
                key: value
                for key, value in (
                    ("project", arguments.project),
                    ("status", arguments.status),
                    ("limit", arguments.limit),
                )
                if value is not None
            }
        )
        return client.request("GET", f"/v1/change-requests?{query}")
    if command == "change-show":
        return client.request("GET", f"/v1/change-requests/{arguments.change_request_id}")
    if command == "change-approve":
        return client.request(
            "POST",
            f"/v1/change-requests/{arguments.change_request_id}/approve",
            {
                "approval_binding": arguments.approval_binding,
                "typed_confirmation": arguments.typed_confirmation,
            },
        )
    if command == "change-revise":
        return client.request(
            "POST",
            f"/v1/change-requests/{arguments.change_request_id}/revise",
            {"note": arguments.note},
        )
    if command == "change-reject":
        return client.request(
            "POST",
            f"/v1/change-requests/{arguments.change_request_id}/reject",
            {"reason": arguments.reason},
        )
    raise ValueError(f"unsupported command: {command}")


def main() -> None:
    arguments = parser().parse_args()
    if arguments.command == "dashboard":
        try:
            _dashboard_password_command(arguments)
        except Exception as error:
            print("engineering-ctl: dashboard password operation failed", file=sys.stderr)
            raise SystemExit(1) from error
        return
    try:
        result = dispatch(APIClient(arguments.api_url, arguments.token_file), arguments)
    except (OSError, ValueError, httpx.HTTPError) as error:
        print(f"engineering-ctl: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, sort_keys=True))


def _dashboard_password_command(arguments: argparse.Namespace) -> None:
    from engineering_control.dashboard.auth import PasswordService, stage_password_output
    from engineering_control.dashboard.repository import DashboardRepository

    output = arguments.output_file
    passwords = PasswordService()
    plaintext = passwords.generate()
    staged = None
    try:
        staged = stage_password_output(
            output,
            plaintext,
            allow_replace=bool(arguments.force_output),
        )
        repository = DashboardRepository(_dashboard_database_url(), password_service=passwords)
        try:
            if arguments.dashboard_command == "init-password":
                repository.initialize_password_sync(plaintext)
            elif arguments.dashboard_command == "reset-password":
                repository.reset_password_sync(plaintext)
            else:
                raise ValueError("unsupported dashboard password operation")
        finally:
            repository.engine.dispose()
        staged.commit()
    except Exception:
        if staged is not None:
            staged.discard()
        raise
    staged.discard()
    print(output)


def _dashboard_database_url() -> str:
    if value := os.getenv("ENGINEERING_DATABASE_URL"):
        return value
    environment_file = Path("/etc/engineering-control/env/control.env")
    descriptor = os.open(environment_file, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
            raise PermissionError("control environment metadata is invalid")
        if stat.S_IMODE(metadata.st_mode) & 0o007:
            raise PermissionError("control environment permissions are too broad")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            for line in stream:
                if line.startswith("ENGINEERING_DATABASE_URL="):
                    value = line.partition("=")[2].strip()
                    if value:
                        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    raise ValueError("ENGINEERING_DATABASE_URL is unavailable")


if __name__ == "__main__":
    main()
