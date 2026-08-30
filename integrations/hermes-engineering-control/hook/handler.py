from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

COMMANDS = {
    "audit",
    "audit-plan",
    "bootstrap-plan",
    "bootstrap",
    "campaigns",
    "findings",
    "loop",
    "enhancements",
    "run",
    "status",
    "evidence",
    "export-campaign",
    "export-workflow",
    "approve",
    "reject",
    "pause",
    "resume",
    "cancel",
    "usage",
    "verify",
    "validate",
    "rollback",
}
API_URL = os.getenv("ENGINEERING_API_URL", "http://127.0.0.1:8740").rstrip("/")
TOKEN_FILE = Path(os.getenv("ENGINEERING_API_TOKEN_FILE", "/etc/engineering-control/env/api.token"))
ALLOWLIST_FILE = Path(
    os.getenv(
        "ENGINEERING_TELEGRAM_ALLOWLIST_FILE",
        "/etc/engineering-control/env/telegram-allowlist",
    )
)
ACCEPTANCE_LOG = Path(
    os.getenv(
        "ENGINEERING_TELEGRAM_ACCEPTANCE_LOG",
        "/home/hermes/engineering-bootstrap/telegram-acceptance.jsonl",
    )
)
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")
_ENHANCEMENT_ID = re.compile(r"^enh-[a-f0-9]{16,64}$")
_PROJECTS = {"falcone", "musematic", "llmwiki"}
_PRESETS = {
    "snapshot",
    "static-full",
    "e2e-smoke",
    "full-e2e",
    "security",
    "ux",
    "deployment",
    "falcone-intensive",
}
_LOOP_MODES = {"plan-only", "source-pr", "preview", "staging"}
_BACKLOG_PROJECTS = {"falcone", "musematic"}
_BACKLOG_FILTERS = {"bugs", "bugs-and-docs", "security", "enhancements", "all"}
_BACKLOG_MODES = {"source-pr", "full-delivery"}
_BACKLOG_REPOSITORIES = {"falcone", "falcone-charts", "musematic", "musematic-deploy"}
_BACKLOG_SKIP_REASONS = {
    "DUPLICATE",
    "ALREADY_FIXED",
    "SUPERSEDED",
    "NEEDS_PRODUCT_DECISION",
    "BLOCKED_BY_DEPENDENCY",
    "BLOCKED_BY_RUNTIME_BASELINE",
    "R3_REQUIRES_OPERATOR",
    "NOT_REPRODUCIBLE",
    "ENHANCEMENT_DEFERRED",
}


def _private_api_url() -> None:
    parsed = urllib.parse.urlparse(API_URL)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Engineering API URL must use loopback HTTP")


def _read_protected(path: Path) -> str:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"protected input is not a regular file: {path.name}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise PermissionError(f"protected input permissions are too broad: {path.name}")
    return path.read_text(encoding="utf-8").strip()


def _authorized(user_id: str) -> bool:
    if not user_id.isdigit():
        return False
    allowed = {
        line.strip()
        for line in _read_protected(ALLOWLIST_FILE).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return user_id in allowed


def _redact(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization|token|password|secret|api[_-]?key)(\s*[:=]\s*)[^\s,}\"]+",
        r"\1\2[REDACTED]",
        value,
    )
    return value[:3500]


def _request(
    method: str,
    path: str,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    _private_api_url()
    token = _read_protected(TOKEN_FILE)
    if len(token) < 32:
        raise ValueError("Engineering API token is not provisioned")
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 - URL is validated as loopback HTTP above
        f"{API_URL}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Engineering-Actor": actor,
            **({"Idempotency-Key": f"telegram-{uuid4()}"} if path == "/v1/commands" else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read(512 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read(16 * 1024).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Engineering API rejected the request ({error.code}): {_redact(detail)}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError("Engineering API is unavailable on loopback") from error


def _gateway_event(
    event_type: str,
    command: str,
    *,
    raw_args: str = "",
    result: Any = None,
    outcome: str | None = None,
) -> None:
    """Best-effort safe event; command text, actor identity, and credentials never leave memory."""
    try:
        _private_api_url()
        token = _read_protected(TOKEN_FILE)
        if len(token) < 32:
            return
        try:
            args = shlex.split(raw_args)
        except ValueError:
            args = []
        project = next((value for value in args[:3] if value in _PROJECTS), None)
        mode = next(
            (
                value
                for value in args[:6]
                if value in (_LOOP_MODES | _BACKLOG_MODES | {"full-delivery"})
            ),
            None,
        )
        issue_number = next(
            (int(value) for value in args[:6] if value.isdigit() and int(value) > 0),
            None,
        )
        result_value = result if isinstance(result, dict) else {}
        workflow_id = next(
            (
                value
                for key in ("workflow_id", "temporal_workflow_id")
                if isinstance((value := result_value.get(key)), str) and value
            ),
            None,
        )
        payload = {
            "event_type": event_type,
            "command": command if command in COMMANDS else "unknown",
            "outcome": outcome,
            "project": project,
            "issue_number": issue_number,
            "mode": mode,
            "workflow_id": workflow_id,
        }
        request = urllib.request.Request(  # noqa: S310 - loopback URL is validated above
            f"{API_URL}/v1/observability/gateway-events",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Engineering-Actor": "hermes-gateway",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            response.read(4096)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError):
        return


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a UUID") from error


def _format(value: Any) -> str:
    return _redact(json.dumps(value, indent=2, sort_keys=True, default=str))


def _acceptance_summary(command: str, raw_args: str, result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if command == "usage":
        providers: dict[str, dict[str, Any]] = {}
        for provider in ("codex", "claude"):
            value = result.get(provider)
            if isinstance(value, dict):
                providers[provider] = {
                    key: value.get(key)
                    for key in (
                        "status",
                        "authenticated",
                        "cli_present",
                        "rate_limited",
                        "running_jobs",
                    )
                }
        return {"providers": providers}
    if command == "validate":
        return {
            key: result.get(key)
            for key in ("workflow_id", "run_id", "status")
            if result.get(key) is not None
        }
    if command == "status":
        args = shlex.split(raw_args)
        validation = result.get("validation")
        safe_validation = (
            {
                key: validation.get(key)
                for key in (
                    "status",
                    "change_set_state",
                    "maker_provider",
                    "checker_provider",
                    "evidence_sha256",
                    "redaction_status",
                )
            }
            if isinstance(validation, dict)
            else None
        )
        return {
            "requested_workflow_id": args[0] if len(args) == 1 else None,
            "workflow_id": result.get("workflow_id"),
            "run_id": result.get("run_id"),
            "status": result.get("status"),
            "workflow_type": result.get("workflow_type"),
            "validation": safe_validation,
        }
    return None


def _record_acceptance(command: str, raw_args: str, result: Any) -> None:
    summary = _acceptance_summary(command, raw_args, result)
    if summary is None:
        return
    release_sha = Path("/opt/engineering-control/source").resolve().parent.name
    serialized_summary = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    event = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "release_sha": release_sha,
        "actor_class": "allowlisted-telegram-user",
        "command": command,
        "result_sha256": hashlib.sha256(serialized_summary.encode()).hexdigest(),
        "summary": summary,
    }
    parent = ACCEPTANCE_LOG.parent
    parent_metadata = parent.stat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError("Telegram acceptance evidence parent is unsafe")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(ACCEPTANCE_LOG, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(ACCEPTANCE_LOG, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise PermissionError("Telegram acceptance evidence file is unsafe")
        if created:
            os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, json.dumps(event, sort_keys=True).encode() + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _dispatch(command: str, raw_args: str, actor: str) -> Any:
    args = shlex.split(raw_args)
    if command in {"campaigns", "findings"}:
        limit = int(args[0]) if args else 20
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return _request("GET", f"/v1/{command}?limit={limit}", actor)
    if command == "usage":
        if args:
            raise ValueError("usage: /usage")
        return _request("GET", "/v1/usage", actor)
    if command == "status":
        if len(args) != 1:
            raise ValueError("usage: /status <workflow-id>")
        return _request("GET", f"/v1/runs/{urllib.parse.quote(args[0], safe='')}", actor)
    if command == "verify":
        if len(args) != 1:
            raise ValueError("usage: /verify <finding-uuid>")
        finding_id = _uuid(args[0], "finding ID")
        return _request("POST", "/v1/findings/verify", actor, {"finding_id": finding_id})
    if command == "validate":
        if len(args) > 1:
            raise ValueError("usage: /validate [hold-seconds]")
        hold_seconds = int(args[0]) if args else 5
        if not 2 <= hold_seconds <= 30:
            raise ValueError("hold-seconds must be between 2 and 30")
        return _request(
            "POST",
            "/v1/validation/lifecycle",
            actor,
            {"hold_seconds": hold_seconds},
        )
    if command == "evidence":
        if len(args) != 1:
            raise ValueError("usage: /evidence <artifact-id>")
        return _request("GET", f"/v1/evidence/{urllib.parse.quote(args[0], safe='')}", actor)
    if command == "export-campaign":
        if len(args) != 1:
            raise ValueError("usage: /export-campaign <campaign-uuid>")
        return _request(
            "POST",
            "/v1/commands",
            actor,
            {
                "action": "evidence_export_campaign",
                "campaign_id": _uuid(args[0], "campaign ID"),
                "format": "json",
            },
        )
    if command == "export-workflow":
        if len(args) != 1 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,240}", args[0]):
            raise ValueError("usage: /export-workflow <workflow-id>")
        return _request(
            "POST",
            "/v1/commands",
            actor,
            {
                "action": "evidence_export_workflow",
                "workflow_id": args[0],
                "format": "json",
            },
        )
    if command in {"pause", "resume", "cancel"}:
        if len(args) != 1:
            raise ValueError(f"usage: /{command} <workflow-id>")
        return _request("POST", "/v1/commands", actor, {"action": command, "workflow_id": args[0]})
    if command == "run":
        if len(args) not in {2, 3} or args[1] not in {"R0", "R1", "R2", "R3"}:
            raise ValueError("usage: /run <finding-uuid> <R0|R1|R2|R3> [--execute]")
        return _request(
            "POST",
            "/v1/deliveries",
            actor,
            {
                "finding_id": _uuid(args[0], "finding ID"),
                "risk_class": args[1],
                "dry_run": "--execute" not in args[2:],
            },
        )
    if command == "audit-plan":
        if len(args) != 3 or args[0] not in _PROJECTS or args[1] not in _PRESETS:
            raise ValueError(
                "usage: /audit-plan <falcone|musematic|llmwiki> <preset> <environment>"
            )
        if args[0] == "llmwiki" and args[2] != "staging":
            raise ValueError("LLMWiki supports staging only")
        if args[1] == "falcone-intensive" and args[0] != "falcone":
            raise ValueError("falcone-intensive is bound to Falcone")
        return _request(
            "POST",
            "/v1/commands",
            actor,
            {
                "action": "audit_plan",
                "project": args[0],
                "preset": args[1],
                "environment": args[2],
            },
        )
    if command == "audit":
        if 3 <= len(args) <= 5 and args[0] in _PROJECTS and args[1] in _PRESETS:
            flags = set(args[3:])
            if flags - {"--execute", "--publish-findings"}:
                raise ValueError("audit flags are not allowlisted")
            if "--publish-findings" in flags and "--execute" not in flags:
                raise ValueError("--publish-findings requires --execute")
            if args[0] == "llmwiki" and args[2] != "staging":
                raise ValueError("LLMWiki supports staging only")
            if args[1] == "falcone-intensive" and args[0] != "falcone":
                raise ValueError("falcone-intensive is bound to Falcone")
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "audit_start" if "--execute" in flags else "audit_plan",
                    "project": args[0],
                    "preset": args[1],
                    "environment": args[2],
                    "publish_findings": "--publish-findings" in flags,
                },
            )
        if len(args) not in {8, 9} or args[0] not in _PROJECTS:
            raise ValueError(
                "usage: /audit <project> <preset> <environment> [--execute] [--publish-findings]"
            )
        source_sha, deployment_sha = args[2], args[3]
        if not _SHA.fullmatch(source_sha) or not _SHA.fullmatch(deployment_sha):
            raise ValueError("source and deployment SHA must be full lowercase Git SHAs")
        return _request(
            "POST",
            "/v1/campaigns",
            actor,
            {
                "project": args[0],
                "name": args[1],
                "source_sha": source_sha,
                "deployment_sha": deployment_sha,
                "domains": [args[4]],
                "personas": [args[5]],
                "lenses": [args[6]],
                "environments": [args[7]],
                "dry_run": len(args) == 8 or args[8] != "--execute",
            },
        )
    if command == "bootstrap-plan":
        if args != ["llmwiki", "staging"]:
            raise ValueError("usage: /bootstrap-plan llmwiki staging")
        return _request(
            "POST",
            "/v1/bootstrap-plan",
            actor,
            {"project": "llmwiki", "environment": "staging"},
        )
    if command == "bootstrap":
        if (
            len(args) != 4
            or args[0:2] != ["llmwiki", "staging"]
            or not _DIGEST.fullmatch(args[2])
            or args[3] != "--execute"
        ):
            raise ValueError("usage: /bootstrap llmwiki staging <binding-sha256> --execute")
        return _request(
            "POST",
            "/v1/bootstrap",
            actor,
            {
                "project": "llmwiki",
                "environment": "staging",
                "approval_binding": args[2],
                "execute": True,
            },
        )
    if command == "loop":
        if not args:
            raise ValueError(
                "usage: /loop <import|inventory|plan|queue|issue|approval-candidate|approve|"
                "start|status|pause|resume|cancel|skip|retry|delivery-candidate|"
                "approve-delivery> ..."
            )
        action = args[0]
        if action == "import":
            if (
                len(args) not in {4, 5}
                or args[1] not in _BACKLOG_PROJECTS
                or args[2] != "github-open"
                or args[3] not in _BACKLOG_FILTERS
            ):
                raise ValueError(
                    "usage: /loop import <falcone|musematic> github-open "
                    "<bugs|bugs-and-docs|security|enhancements|all> [max-items]"
                )
            maximum_items = int(args[4]) if len(args) == 5 else 500
            if not 1 <= maximum_items <= 500:
                raise ValueError("backlog import max-items must be between 1 and 500")
            return _request(
                "POST",
                "/v1/backlogs/import",
                actor,
                {
                    "project": args[1],
                    "source": "github-open",
                    "import_filter": args[3],
                    "maximum_items": maximum_items,
                },
            )
        if action == "inventory":
            if len(args) not in {3, 4} or args[1] not in _BACKLOG_PROJECTS:
                raise ValueError(
                    "usage: /loop inventory <falcone|musematic> <backlog-campaign-id> [limit]"
                )
            campaign_id = _uuid(args[2], "backlog campaign ID")
            limit = int(args[3]) if len(args) == 4 else 20
            if not 1 <= limit <= 200:
                raise ValueError("inventory limit must be between 1 and 200")
            return _request("GET", f"/v1/backlogs/{campaign_id}/issues?limit={limit}", actor)
        if action == "plan":
            if len(args) != 3 or args[1] not in _PROJECTS:
                raise ValueError("usage: /loop plan <project> <campaign-id>")
            campaign_id = _uuid(args[2], "backlog campaign ID")
            return _request(
                "POST",
                "/v1/loop-plan",
                actor,
                {"project": args[1], "campaign_id": campaign_id},
            )
        if action == "queue":
            if len(args) not in {2, 3}:
                raise ValueError("usage: /loop queue <backlog-campaign-id> [limit]")
            campaign_id = _uuid(args[1], "backlog campaign ID")
            limit = int(args[2]) if len(args) == 3 else 20
            if not 1 <= limit <= 200:
                raise ValueError("queue limit must be between 1 and 200")
            return _request("GET", f"/v1/backlogs/{campaign_id}/issues?limit={limit}", actor)
        if action == "issue":
            if (
                len(args) != 4
                or args[2] not in _BACKLOG_REPOSITORIES
                or not args[3].isdigit()
                or int(args[3]) < 1
            ):
                raise ValueError(
                    "usage: /loop issue <backlog-campaign-id> <repository-short-name> "
                    "<issue-number>"
                )
            campaign_id = _uuid(args[1], "backlog campaign ID")
            return _request(
                "GET",
                f"/v1/backlogs/{campaign_id}/issues/{args[2]}/{int(args[3])}",
                actor,
            )
        if action == "approval-candidate":
            if (
                len(args) not in {5, 6}
                or args[1] not in _BACKLOG_PROJECTS
                or args[3] not in _BACKLOG_MODES
                or args[4] not in {"R0", "R1", "R2", "R3"}
            ):
                raise ValueError(
                    "usage: /loop approval-candidate <project> <backlog-campaign-id> "
                    "<source-pr|full-delivery> <max-risk> [max-items]"
                )
            campaign_id = _uuid(args[2], "backlog campaign ID")
            maximum_items = int(args[5]) if len(args) == 6 else 1
            if not 1 <= maximum_items <= 50:
                raise ValueError("approval max-items must be between 1 and 50")
            return _request(
                "POST",
                f"/v1/backlogs/{campaign_id}/approval-candidate",
                actor,
                {
                    "project": args[1],
                    "delivery_mode": args[3],
                    "maximum_risk": args[4],
                    "maximum_items": maximum_items,
                },
            )
        if action == "approve":
            if (
                len(args) != 5
                or args[1] not in _BACKLOG_PROJECTS
                or not _DIGEST.fullmatch(args[3])
                or args[4] != "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP"
            ):
                raise ValueError(
                    "usage: /loop approve <project> <backlog-campaign-id> <binding-sha256> "
                    "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP"
                )
            campaign_id = _uuid(args[2], "backlog campaign ID")
            return _request(
                "POST",
                f"/v1/backlogs/{campaign_id}/approve",
                actor,
                {
                    "project": args[1],
                    "approval_binding": args[3],
                    "typed_confirmation": args[4],
                },
            )
        if action == "start":
            if (
                len(args) not in {5, 6}
                or args[1] not in _PROJECTS
                or args[3] not in {"R0", "R1", "R2", "R3"}
                or args[4] not in {*_BACKLOG_MODES, *_LOOP_MODES}
            ):
                raise ValueError(
                    "usage: /loop start <project> <campaign-id> <max-risk> "
                    "<source-pr|full-delivery> [max-items]"
                )
            maximum_items = int(args[5]) if len(args) == 6 else 1
            if not 1 <= maximum_items <= 50:
                raise ValueError("max-items must be between 1 and 50")
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "solve_backlog",
                    "project": args[1],
                    "backlog_campaign_id": _uuid(args[2], "campaign ID"),
                    "maximum_risk": args[3],
                    "maximum_items": maximum_items,
                    "delivery_mode": args[4],
                },
            )
        if action == "status":
            if len(args) != 2:
                raise ValueError("usage: /loop status <workflow-id>")
            workflow_id = urllib.parse.quote(args[1], safe="")
            return _request("GET", f"/v1/loops/{workflow_id}", actor)
        if action in {"pause", "resume", "cancel"}:
            if len(args) != 2:
                raise ValueError(f"usage: /loop {action} <workflow-id>")
            return _request(
                "POST", "/v1/commands", actor, {"action": action, "workflow_id": args[1]}
            )
        if action == "skip":
            if (
                len(args) != 5
                or args[2] not in _BACKLOG_REPOSITORIES
                or not args[3].isdigit()
                or int(args[3]) < 1
                or args[4] not in _BACKLOG_SKIP_REASONS
            ):
                raise ValueError(
                    "usage: /loop skip <workflow-id> <repository-short-name> "
                    "<issue-number> <reason-code>"
                )
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "skip",
                    "workflow_id": args[1],
                    "repository": args[2],
                    "issue_number": int(args[3]),
                    "reason_code": args[4],
                },
            )
        if action == "retry":
            if (
                len(args) != 4
                or args[2] not in _BACKLOG_REPOSITORIES
                or not args[3].isdigit()
                or int(args[3]) < 1
            ):
                raise ValueError(
                    "usage: /loop retry <workflow-id> <repository-short-name> <issue-number>"
                )
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "retry",
                    "workflow_id": args[1],
                    "repository": args[2],
                    "issue_number": int(args[3]),
                },
            )
        if action == "delivery-candidate":
            if (
                len(args) != 4
                or args[2] not in _BACKLOG_REPOSITORIES
                or not args[3].isdigit()
                or int(args[3]) < 1
            ):
                raise ValueError(
                    "usage: /loop delivery-candidate <workflow-id> <repository-short-name> "
                    "<issue-number>"
                )
            workflow_id = urllib.parse.quote(args[1], safe="")
            return _request(
                "GET",
                f"/v1/backlog-loops/{workflow_id}/delivery-candidate?"
                f"repository={args[2]}&issue_number={int(args[3])}",
                actor,
            )
        if action == "approve-delivery":
            if (
                len(args) != 4
                or not _DIGEST.fullmatch(args[2])
                or args[3] != "APPROVE_STAGING_ISSUE_DELIVERY"
            ):
                raise ValueError(
                    "usage: /loop approve-delivery <workflow-id> <binding-sha256> "
                    "APPROVE_STAGING_ISSUE_DELIVERY"
                )
            workflow_id = urllib.parse.quote(args[1], safe="")
            return _request(
                "POST",
                f"/v1/backlog-loops/{workflow_id}/approve-delivery",
                actor,
                {"approval_binding": args[2], "typed_confirmation": args[3]},
            )
        raise ValueError("unsupported /loop action")
    if command == "enhancements":
        if not args:
            raise ValueError("usage: /enhancements <discover|list|show|approve|reject|run> ...")
        action = args[0]
        if action == "discover":
            if len(args) not in {3, 4} or args[1] not in _PROJECTS:
                raise ValueError(
                    "usage: /enhancements discover <project> <campaign-id> [--execute]"
                )
            if len(args) == 4 and args[3] != "--execute":
                raise ValueError("enhancement discovery flag is not allowlisted")
            return _request(
                "POST",
                "/v1/enhancements/discover",
                actor,
                {
                    "project": args[1],
                    "campaign_id": _uuid(args[2], "campaign ID"),
                    "dry_run": len(args) == 3,
                },
            )
        if action == "list":
            if len(args) not in {2, 3} or args[1] not in _PROJECTS:
                raise ValueError("usage: /enhancements list <project> [status]")
            query = urllib.parse.urlencode(
                {"project": args[1], **({"status": args[2]} if len(args) == 3 else {})}
            )
            return _request("GET", f"/v1/enhancements?{query}", actor)
        if action == "show":
            if len(args) != 2 or not _ENHANCEMENT_ID.fullmatch(args[1]):
                raise ValueError("usage: /enhancements show <enhancement-id>")
            return _request("GET", f"/v1/enhancements/{args[1]}", actor)
        if action == "approve":
            if (
                len(args) != 3
                or not _ENHANCEMENT_ID.fullmatch(args[1])
                or not _DIGEST.fullmatch(args[2])
            ):
                raise ValueError("usage: /enhancements approve <enhancement-id> <binding>")
            return _request(
                "POST",
                f"/v1/enhancements/{args[1]}/approve",
                actor,
                {"approval_binding": args[2]},
            )
        if action == "reject":
            if len(args) < 3 or not _ENHANCEMENT_ID.fullmatch(args[1]):
                raise ValueError("usage: /enhancements reject <enhancement-id> <reason>")
            return _request(
                "POST",
                f"/v1/enhancements/{args[1]}/reject",
                actor,
                {"reason": " ".join(args[2:])},
            )
        if action == "run":
            if (
                len(args) not in {4, 5}
                or not _ENHANCEMENT_ID.fullmatch(args[1])
                or args[2] not in {"R0", "R1", "R2", "R3"}
                or args[3] not in _LOOP_MODES
                or (len(args) == 5 and args[4] != "--execute")
            ):
                raise ValueError(
                    "usage: /enhancements run <enhancement-id> <risk> <mode> [--execute]"
                )
            record = _request("GET", f"/v1/enhancements/{args[1]}", actor)
            binding = record.get("approval_binding_sha256") if isinstance(record, dict) else None
            if not isinstance(binding, str) or not _DIGEST.fullmatch(binding):
                raise ValueError("approved enhancement binding is unavailable")
            return _request(
                "POST",
                f"/v1/enhancements/{args[1]}/run",
                actor,
                {
                    "approval_binding": binding,
                    "risk": args[2],
                    "execution_mode": args[3],
                    "dry_run": len(args) == 4,
                },
            )
        raise ValueError("unsupported /enhancements action")
    if command in {"approve", "reject"}:
        if not args:
            raise ValueError(f"usage: /{command} <change-set-uuid> [digest] [confirmation]")
        change_set_id = _uuid(args[0], "ChangeSet ID")
        if len(args) == 1:
            return _request("GET", f"/v1/approvals/{change_set_id}/candidate", actor)
        if not _DIGEST.fullmatch(args[1]):
            raise ValueError("artifact binding must be a 64-character SHA256")
        return _request(
            "POST",
            "/v1/approvals",
            actor,
            {
                "change_set_id": change_set_id,
                "artifact_binding_sha256": args[1],
                "decision": command,
                "typed_confirmation": " ".join(args[2:]) or None,
            },
        )
    if command == "rollback":
        if len(args) not in {2, 3}:
            raise ValueError("usage: /rollback <deployment-uuid> <binding-sha256> [--execute]")
        if not _DIGEST.fullmatch(args[1]):
            raise ValueError("artifact binding must be a 64-character SHA256")
        return _request(
            "POST",
            "/v1/deployments/rollback",
            actor,
            {
                "deployment_id": _uuid(args[0], "deployment ID"),
                "expected_artifact_binding_sha256": args[1],
                "dry_run": "--execute" not in args[2:],
            },
        )
    raise ValueError("unsupported Engineering Control command")


def handle(event_type: str, context: dict[str, Any]) -> dict[str, str] | None:
    command = event_type.removeprefix("command:").replace("_", "-")
    if command not in COMMANDS:
        if event_type.startswith("command:"):
            _gateway_event("telegram.command_rejected", "unknown", outcome="UNKNOWN_COMMAND")
            return {"decision": "deny", "message": "Unknown engineering command denied."}
        return None
    _gateway_event("telegram.command_received", command)
    _gateway_event("telegram.command_recognized", command)
    if context.get("platform") != "telegram":
        _gateway_event("telegram.command_rejected", command, outcome="INVALID_PLATFORM")
        return {
            "decision": "deny",
            "message": "Engineering Control chat commands are restricted to Telegram.",
        }
    user_id = str(context.get("user_id", ""))
    try:
        if not _authorized(user_id):
            _gateway_event("telegram.command_rejected", command, outcome="ACCESS_DENIED")
            return {"decision": "deny", "message": "Engineering Control access denied."}
        raw_args = str(context.get("raw_args", ""))
        _gateway_event("engineering.request_accepted", command, raw_args=raw_args)
        _gateway_event("engineering.api_request_started", command, raw_args=raw_args)
        result = _dispatch(command, raw_args, f"telegram:{user_id}")
        _gateway_event(
            "engineering.api_response_completed",
            command,
            raw_args=raw_args,
            result=result,
            outcome="COMPLETED",
        )
        if isinstance(result, dict) and isinstance(result.get("workflow_id"), str):
            _gateway_event(
                "engineering.workflow_returned", command, raw_args=raw_args, result=result
            )
        _record_acceptance(command, raw_args, result)
        _gateway_event("telegram.response_delivered", command, raw_args=raw_args, result=result)
        return {"decision": "handled", "message": _format(result)}
    except (OSError, RuntimeError, ValueError) as error:
        _gateway_event("telegram.response_delivery_failed", command, outcome="REQUEST_FAILED")
        return {"decision": "handled", "message": _redact(f"Engineering Control: {error}")}
