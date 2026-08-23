from __future__ import annotations

import json
import os
import re
import shlex
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

COMMANDS = {
    "audit",
    "campaigns",
    "findings",
    "run",
    "status",
    "evidence",
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
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")


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


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a UUID") from error


def _format(value: Any) -> str:
    return _redact(json.dumps(value, indent=2, sort_keys=True, default=str))


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
    if command in {"pause", "resume"}:
        if len(args) != 1:
            raise ValueError(f"usage: /{command} <workflow-id>")
        workflow_id = urllib.parse.quote(args[0], safe="")
        return _request("POST", f"/v1/campaigns/{workflow_id}/{command}", actor, {})
    if command == "cancel":
        if len(args) != 1:
            raise ValueError("usage: /cancel <workflow-id>")
        workflow_id = urllib.parse.quote(args[0], safe="")
        return _request("POST", f"/v1/runs/{workflow_id}/cancel", actor, {})
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
    if command == "audit":
        if len(args) not in {8, 9} or args[0] not in {"falcone", "musematic"}:
            raise ValueError(
                "usage: /audit <falcone|musematic> <name> <source-sha> <deploy-sha> "
                "<domain> <persona> <lens> <environment> [--execute]"
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
        return None
    if context.get("platform") != "telegram":
        return {
            "decision": "deny",
            "message": "Engineering Control chat commands are restricted to Telegram.",
        }
    user_id = str(context.get("user_id", ""))
    try:
        if not _authorized(user_id):
            return {"decision": "deny", "message": "Engineering Control access denied."}
        result = _dispatch(command, str(context.get("raw_args", "")), f"telegram:{user_id}")
        return {"decision": "handled", "message": _format(result)}
    except (OSError, RuntimeError, ValueError) as error:
        return {"decision": "handled", "message": _redact(f"Engineering Control: {error}")}
