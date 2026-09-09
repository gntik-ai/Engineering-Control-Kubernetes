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
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

COMMANDS = {
    "audit",
    "audit-all",
    "audit-plan",
    "bootstrap-plan",
    "bootstrap",
    "campaigns",
    "campaign",
    "ready",
    "connections",
    "temporal",
    "providers",
    "workers",
    "channel-check",
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
    "documentation",
    "falcone-intensive",
}
_LOOP_MODES = {"plan-only", "source-pr", "preview", "staging"}
_BACKLOG_PROJECTS = {"falcone", "musematic", "llmwiki"}
_BACKLOG_FILTERS = {"bugs", "bugs-and-docs", "security", "enhancements", "all"}
_BACKLOG_MODES = {"source-pr", "full-delivery"}
_BACKLOG_REPOSITORIES = {
    "falcone",
    "falcone-charts",
    "musematic",
    "musematic-deploy",
    "llmwiki",
}
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
_MESSAGE_LIMIT = 3500
_PAGE_CACHE_MAX_ENTRIES = 256
_CURSOR_PLACEHOLDER = "0" * 32
_PAGE_CACHE: dict[str, tuple[datetime, str, str, list[Any]]] = {}
_RECEIPT_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("receipt_context", default=None)
_EVENT_OUTBOX: Any = None
_EVENT_TYPES = {
    "telegram.command_received",
    "telegram.command_recognized",
    "telegram.command_rejected",
    "engineering.request_accepted",
    "engineering.api_request_started",
    "engineering.api_response_completed",
    "engineering.workflow_returned",
    "engineering.request_failed",
    "telegram.response_prepared",
    "telegram.response_delivered",
    "telegram.response_delivery_failed",
}


def bind_event_outbox(outbox: Any) -> None:
    global _EVENT_OUTBOX
    if _EVENT_OUTBOX is not None or not callable(getattr(outbox, "enqueue", None)):
        raise ValueError("durable event outbox binding is invalid")
    _EVENT_OUTBOX = outbox


class _NoEventRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def deliver_event(payload: dict[str, Any], observed_at: str) -> dict[str, str]:
    """Fixed private consumer only. No proxy/redirect or response-body error log."""
    try:
        _private_api_url()
        token = _read_protected(TOKEN_FILE)
        if len(token) < 32:
            return {"status": "RETRY"}
        body = {**payload, "event_observed_at": observed_at}
        request = urllib.request.Request(  # noqa: S310 - fixed loopback endpoint, redirects/proxies disabled
            f"{API_URL}/v1/observability/gateway-events",
            method="POST",
            data=json.dumps(body, separators=(",", ":"), allow_nan=False).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Engineering-Actor": "hermes-gateway",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoEventRedirect())
        with opener.open(request, timeout=2) as response:  # noqa: S310
            if response.status != 202:
                return {"status": "RETRY"}
            raw = response.read(4097)
            if len(raw) > 4096:
                return {"status": "RETRY"}
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or result.get("status") != "ACCEPTED"
                or type(result.get("duplicate")) is not bool
            ):
                return {"status": "RETRY"}
            # DashboardEvent.id is a positive BIGINT, not a workflow UUID.
            receipt = result.get("event_id")
            if type(receipt) is not int or not 0 < receipt <= 2**63 - 1:
                return {"status": "RETRY"}
            return {"status": "ACKNOWLEDGED", "receipt": str(receipt)}
    except urllib.error.HTTPError as error:
        error.close()
        return {"status": "BLOCKED" if error.code in {400, 409, 422} else "RETRY"}
    except (OSError, RuntimeError, ValueError, urllib.error.URLError):
        return {"status": "RETRY"}


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
    return value


def _safe_error(value: str) -> str:
    redacted = _redact(value)
    if len(redacted) <= _MESSAGE_LIMIT:
        return redacted
    return (
        "Engineering Control request failed; the bounded error is available by digest "
        f"{hashlib.sha256(redacted.encode()).hexdigest()}."
    )


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
            **({"Idempotency-Key": _command_idempotency_key()} if path == "/v1/commands" else {}),
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


def _event_payload(
    event_type: str,
    command: str,
    *,
    raw_args: str = "",
    result: Any = None,
    outcome: str | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build minimized metadata; no configuration reads or persistence."""
    if event_type not in _EVENT_TYPES:
        raise ValueError("invalid gateway event type")
    details = dict(receipt or {})
    identity = _receipt_identity({**(_RECEIPT_CONTEXT.get() or {}), **details})
    if not identity:
        raise ValueError("gateway event correlation is unavailable")
    if outcome is not None and not re.fullmatch(r"[A-Z0-9_]{1,40}", outcome):
        raise ValueError("invalid gateway event outcome")
    try:
        args = shlex.split(raw_args[:65536])
    except ValueError:
        args = []
    project = next((value for value in args[:3] if value in _PROJECTS), None)
    mode = next((value for value in args[:6] if value in (_LOOP_MODES | _BACKLOG_MODES)), None)
    issue_number = next(
        (
            int(value)
            for value in args[:6]
            if value.isascii()
            and value.isdigit()
            and len(value) <= 10
            and 0 < int(value) <= 2**31 - 1
        ),
        None,
    )
    result_value = result if isinstance(result, dict) else {}
    workflow_id = next(
        (
            value
            for key in ("workflow_id", "temporal_workflow_id")
            if isinstance((value := result_value.get(key)), str)
            and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,240}", value)
        ),
        None,
    )
    delivery_status = details.get("delivery_status")
    ids = details.get("acknowledged_message_ids", [])
    if delivery_status is not None and delivery_status not in {
        "ACKNOWLEDGED",
        "UNACKNOWLEDGED",
        "UNKNOWN",
        "UNAVAILABLE",
        "EMPTY",
        "REJECTED_OVERSIZE",
    }:
        raise ValueError("invalid delivery status")
    if (
        not isinstance(ids, list)
        or len(ids) > 64
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) for value in ids
        )
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("invalid delivery identifiers")
    if event_type == "telegram.response_delivered":
        if delivery_status != "ACKNOWLEDGED" or not ids:
            raise ValueError("complete delivery receipt required")
    elif delivery_status == "ACKNOWLEDGED" or ids:
        raise ValueError("unexpected delivery acknowledgement")
    payload = dict(
        event_type=event_type,
        command=command if command in COMMANDS else "unknown",
        outcome=outcome,
        project=project,
        issue_number=issue_number,
        mode=mode,
        workflow_id=workflow_id,
        **identity,
        delivery_status=delivery_status,
        acknowledged_message_ids=ids,
    )
    return payload


def _gateway_event(event_type: str, command: str, **kwargs: Any) -> None:
    if _EVENT_OUTBOX is None:
        raise RuntimeError("durable gateway event storage is unavailable")
    payload = _event_payload(event_type, command, **kwargs)
    _EVENT_OUTBOX.enqueue(payload["delivery_correlation_id"] + ":" + event_type, payload)


def prepare_terminal_event(context: dict[str, Any]) -> dict[str, Any]:
    payload = _transport_receipt(context, persist=False)
    if payload is None:
        raise ValueError("terminal event identity or authorization unavailable")
    return {
        "identity": payload["delivery_correlation_id"] + ":" + payload["event_type"],
        "payload": payload,
    }


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a UUID") from error


def _located_issue(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError("located issue projection is invalid")
    campaign_id = value.get("backlog_campaign_id")
    issue = value.get("issue")
    repository = issue.get("repository") if isinstance(issue, dict) else None
    if (
        not isinstance(campaign_id, str)
        or not isinstance(repository, str)
        or not repository.startswith("gntik-ai/")
        or repository.removeprefix("gntik-ai/") not in _BACKLOG_REPOSITORIES
    ):
        raise ValueError("located issue projection is invalid")
    return _uuid(campaign_id, "backlog campaign ID"), repository


def _page_token() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()[:32]


def _rendered_page(payload: Any, cursor: str | None) -> str:
    return _redact(
        json.dumps(
            {"payload": payload, "next_cursor": cursor},
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _page_fits(payload: Any, *, has_next: bool) -> bool:
    cursor = _CURSOR_PLACEHOLDER if has_next else None
    return len(_rendered_page(payload, cursor)) <= _MESSAGE_LIMIT


def _oversized_record(value: Any) -> dict[str, Any]:
    serialized = _redact(json.dumps(value, indent=2, sort_keys=True, default=str))
    return {
        "oversized_record": True,
        "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "rendered_characters": len(serialized),
    }


def _bounded_record(value: Any) -> Any:
    if _page_fits(value, has_next=True):
        return value
    if isinstance(value, dict):
        bounded = {
            key: (
                {
                    "omitted": True,
                    "sha256": hashlib.sha256(str(item).encode()).hexdigest(),
                    "rendered_characters": len(str(item)),
                }
                if len(str(item)) > 500
                else item
            )
            for key, item in value.items()
        }
        if _page_fits(bounded, has_next=True):
            return bounded
    return _oversized_record(value)


def _page_payloads(value: Any) -> list[Any]:
    if isinstance(value, dict):
        list_key = next((key for key, item in value.items() if isinstance(item, list)), None)
        if list_key is not None:
            base = {key: item for key, item in value.items() if key != list_key}
            records = value[list_key]
            if not records:
                return [value]
            if not _page_fits({**base, list_key: []}, has_next=True):
                return [_oversized_record(value)]
            pages: list[Any] = []
            current: list[Any] = []
            for index, record in enumerate(records):
                bounded = _bounded_record(record)
                singleton = {**base, list_key: [bounded]}
                if not _page_fits(singleton, has_next=True):
                    bounded = _oversized_record(record)
                    singleton = {**base, list_key: [bounded]}
                if not _page_fits(singleton, has_next=True):
                    return [_oversized_record(value)]
                candidate = {**base, list_key: [*current, bounded]}
                has_next = index < len(records) - 1
                if current and not _page_fits(candidate, has_next=has_next):
                    pages.append({**base, list_key: current})
                    current = [bounded]
                else:
                    current.append(bounded)
            pages.append({**base, list_key: current})
            for index, page in enumerate(pages):
                if not _page_fits(page, has_next=index < len(pages) - 1):
                    raise ValueError("one bounded Telegram record cannot fit in a rendered page")
            return pages
    if isinstance(value, list):
        return _page_payloads({"items": value})
    return [_bounded_record(value)]


def _render_page(
    payload: Any,
    *,
    command: str,
    actor: str,
    remaining: list[Any],
) -> str:
    cursor = _page_token() if remaining else None
    rendered = _rendered_page(payload, cursor)
    if len(rendered) > _MESSAGE_LIMIT:
        raise ValueError("bounded Telegram page unexpectedly exceeds 3500 characters")
    if cursor is not None:
        _sweep_page_cache(datetime.now(UTC))
        while len(_PAGE_CACHE) >= _PAGE_CACHE_MAX_ENTRIES:
            oldest = min(_PAGE_CACHE, key=lambda key: _PAGE_CACHE[key][0])
            _PAGE_CACHE.pop(oldest, None)
        _PAGE_CACHE[cursor] = (
            datetime.now(UTC) + timedelta(minutes=10),
            command,
            actor,
            remaining,
        )
    return rendered


def _sweep_page_cache(now: datetime) -> None:
    for key, (expires_at, _command, _actor, _pages) in list(_PAGE_CACHE.items()):
        if expires_at <= now:
            _PAGE_CACHE.pop(key, None)


def _format(value: Any, *, command: str, actor: str) -> str:
    _sweep_page_cache(datetime.now(UTC))
    pages = _page_payloads(value)
    return _render_page(pages[0], command=command, actor=actor, remaining=pages[1:])


def _cursor_page(token: str, *, command: str, actor: str) -> str:
    now = datetime.now(UTC)
    _sweep_page_cache(now)
    cached = _PAGE_CACHE.get(token)
    if cached is None:
        raise ValueError("pagination cursor is invalid or expired")
    expires_at, expected_command, expected_actor, pages = cached
    if expires_at <= now or expected_command != command or expected_actor != actor or not pages:
        raise ValueError("pagination cursor is invalid or expired")
    rendered = _render_page(pages[0], command=command, actor=actor, remaining=pages[1:])
    _PAGE_CACHE.pop(token, None)
    return rendered


def _split_cursor(raw_args: str) -> tuple[str, str | None]:
    args = shlex.split(raw_args)
    if "--cursor" not in args:
        return raw_args, None
    index = args.index("--cursor")
    if index + 2 != len(args) or not re.fullmatch(r"[a-f0-9]{32}", args[index + 1]):
        raise ValueError("pagination cursor is malformed")
    return shlex.join(args[:index]), args[index + 1]


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
    if command == "campaign":
        if len(args) != 1:
            raise ValueError("usage: /campaign <campaign-id>")
        return _request("GET", f"/v1/campaign-detail/{_uuid(args[0], 'campaign ID')}", actor)
    if command == "ready":
        if len(args) != 1 or args[0] not in {"core", "all"}:
            raise ValueError("usage: /ready <core|all>")
        return _request("GET", f"/v1/readiness/{args[0]}", actor)
    if command == "connections":
        if args != ["all"]:
            raise ValueError("usage: /connections all")
        return _request("GET", "/v1/connections?scope=all", actor)
    if command == "temporal":
        if args != ["summary"]:
            raise ValueError("usage: /temporal summary")
        return _request("GET", "/v1/temporal/summary", actor)
    if command == "providers":
        if args:
            raise ValueError("usage: /providers")
        return _request("GET", "/v1/providers", actor)
    if command == "workers":
        if args != ["all"]:
            raise ValueError("usage: /workers all")
        return _request("GET", "/v1/workers", actor)
    if command == "channel-check":
        if args:
            raise ValueError("usage: /channel-check")
        return _request("GET", "/v1/channel-check", actor)
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
    if command == "audit-all":
        if (
            len(args) not in {3, 4}
            or args[0] not in {"plan", "start"}
            or args[1] not in _PRESETS - {"falcone-intensive"}
            or args[2] != "staging"
            or (len(args) == 4 and args[3] != "--publish-findings")
            or (args[0] == "plan" and len(args) == 4)
        ):
            raise ValueError("usage: /audit-all <plan|start> <preset> staging [--publish-findings]")
        return _request(
            "POST",
            "/v1/commands",
            actor,
            {
                "action": "audit_all_projects",
                "mode": args[0],
                "preset": args[1],
                "environment": "staging",
                "publish_findings": len(args) == 4,
                "production": False,
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
        if action == "global-start":
            if len(args) != 7 or args[6] != "START_GLOBAL_STAGING_R2_BACKLOG":
                raise ValueError(
                    "usage: /loop global-start <global-campaign-id> <policy-campaign-id> "
                    "<falcone-campaign-id> <musematic-campaign-id> <llmwiki-campaign-id> "
                    "START_GLOBAL_STAGING_R2_BACKLOG"
                )
            values = [_uuid(value, "campaign ID") for value in args[1:6]]
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "start_autonomous_backlog",
                    "campaign_id": values[0],
                    "policy_campaign_id": values[1],
                    "backlog_campaign_ids": {
                        "falcone": values[2],
                        "musematic": values[3],
                        "llmwiki": values[4],
                    },
                    "environment": "staging",
                    "maximum_risk": "R2",
                    "global_active_issues": 1,
                    "global_deployments": 1,
                    "implementation_correction_limit": 5,
                    "deployment_correction_limit": 3,
                    "production": False,
                    "typed_confirmation": args[6],
                },
            )
        if action in {"issue-locate", "issue-start"}:
            if (
                (action == "issue-locate" and len(args) != 4)
                or (action == "issue-start" and len(args) not in {4, 5})
                or args[1] not in _PROJECTS
                or args[2] not in {"source", "deployment"}
                or not args[3].isdigit()
                or int(args[3]) < 1
                or (
                    action == "issue-start"
                    and len(args) == 5
                    and args[4] not in {"source-pr", "full-delivery"}
                )
            ):
                raise ValueError(
                    f"usage: /loop {action} <project> <source|deployment> <issue-number>"
                    + (" [source-pr|full-delivery]" if action == "issue-start" else "")
                )
            query = urllib.parse.urlencode(
                {"project": args[1], "surface": args[2], "issue_number": int(args[3])}
            )
            located = _request("GET", f"/v1/backlogs/locate?{query}", actor)
            if action == "issue-locate":
                return located
            campaign_id, repository = _located_issue(located)
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "solve_issue",
                    "project": args[1],
                    "backlog_campaign_id": campaign_id,
                    "repository": repository,
                    "issue_number": int(args[3]),
                    "maximum_risk": "R2",
                    "delivery_mode": args[4] if len(args) == 5 else "source-pr",
                    "correction_limit": 5,
                    "production": False,
                },
            )
        if action in {"diagnose", "tail", "nudge"}:
            expected = 3 if action == "tail" else 2
            if len(args) != expected:
                suffix = " <1-100>" if action == "tail" else ""
                raise ValueError(f"usage: /loop {action} <workflow-id>{suffix}")
            if action == "diagnose":
                return _request("GET", f"/v1/runs/{urllib.parse.quote(args[1], safe='')}", actor)
            if action == "tail":
                limit = int(args[2])
                if not 1 <= limit <= 100:
                    raise ValueError("tail limit must be between 1 and 100")
                workflow_id = urllib.parse.quote(args[1], safe="")
                return _request("GET", f"/v1/workflows/{workflow_id}/tail?limit={limit}", actor)
            return _request(
                "POST", "/v1/commands", actor, {"action": "nudge", "workflow_id": args[1]}
            )
        if action == "retry-issue":
            if (
                len(args) != 5
                or args[2] not in _PROJECTS
                or args[3] not in {"source", "deployment"}
                or not args[4].isdigit()
                or int(args[4]) < 1
            ):
                raise ValueError(
                    "usage: /loop retry-issue <workflow-id> <project> "
                    "<source|deployment> <issue-number>"
                )
            query = urllib.parse.urlencode(
                {"project": args[2], "surface": args[3], "issue_number": int(args[4])}
            )
            located = _request("GET", f"/v1/backlogs/locate?{query}", actor)
            _campaign_id, full_repository = _located_issue(located)
            repository = full_repository.removeprefix("gntik-ai/")
            return _request(
                "POST",
                "/v1/commands",
                actor,
                {
                    "action": "retry",
                    "workflow_id": args[1],
                    "repository": repository,
                    "issue_number": int(args[4]),
                },
            )
        if action in {"r3-candidate", "approve-r3"}:
            expected = 6 if action == "approve-r3" else 4
            if len(args) != expected or args[1:4] != ["musematic", "source", "1119"]:
                suffix = " <binding> <confirmation-phrase>" if action == "approve-r3" else ""
                raise ValueError(f"usage: /loop {action} musematic source 1119{suffix}")
            if action == "r3-candidate":
                query = urllib.parse.urlencode(
                    {"project": args[1], "surface": args[2], "issue_number": int(args[3])}
                )
                return _request("POST", f"/v1/r3-issues/candidate?{query}", actor)
            if not _DIGEST.fullmatch(args[4]) or not re.fullmatch(
                r"APPROVE_R3_[A-Z]+_[1-9][0-9]*_[A-F0-9]{16}", args[5]
            ):
                raise ValueError("R3 approval binding or confirmation phrase is malformed")
            return _request(
                "POST",
                "/v1/r3-issues/approve",
                actor,
                {
                    "project": args[1],
                    "surface": args[2],
                    "issue_number": int(args[3]),
                    "binding": args[4],
                    "confirmation_phrase": args[5],
                },
            )
        if action == "import":
            if (
                len(args) not in {4, 5}
                or args[1] not in _BACKLOG_PROJECTS
                or args[2] != "github-open"
                or args[3] not in _BACKLOG_FILTERS
            ):
                raise ValueError(
                    "usage: /loop import <falcone|musematic|llmwiki> github-open "
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
                    "usage: /loop inventory <falcone|musematic|llmwiki> "
                    "<backlog-campaign-id> [limit]"
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


def _command_idempotency_key() -> str:
    context = _RECEIPT_CONTEXT.get() or {}
    correlation = context.get("delivery_correlation_id")
    if context.get("platform_update_id") is not None and correlation:
        return f"telegram-{correlation}"
    # Legacy callers have no platform update identity. Do not pretend their
    # random key protects duplicate updates across gateway restarts.
    return f"telegram-{uuid4()}"


def _receipt_identity(context: dict[str, Any]) -> dict[str, Any]:
    update_id = context.get("platform_update_id")
    correlation = context.get("delivery_correlation_id")
    if type(update_id) is not int or not 0 <= update_id <= 2**53 - 1:
        return {}
    try:
        correlation = str(UUID(str(correlation)))
    except ValueError:
        return {}
    return {"platform_update_id": update_id, "delivery_correlation_id": correlation}


def _transport_receipt(context: dict[str, Any], *, persist: bool = True) -> dict[str, Any] | None:
    command = context.get("command")
    identity = _receipt_identity(context)
    if context.get("platform") != "telegram" or command not in COMMANDS or not identity:
        return
    try:
        if not _authorized(str(context.get("user_id", ""))):
            return
    except (OSError, ValueError):
        return
    status = context.get("delivery_status")
    allowed = {
        "ACKNOWLEDGED",
        "UNACKNOWLEDGED",
        "UNKNOWN",
        "UNAVAILABLE",
        "EMPTY",
        "REJECTED_OVERSIZE",
    }
    if status not in allowed:
        return
    ids = context.get("acknowledged_message_ids")
    valid_ids = (
        isinstance(ids, list)
        and 1 <= len(ids) <= 64
        and all(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) for value in ids)
        and len(set(ids)) == len(ids)
    )
    if status == "ACKNOWLEDGED" and not valid_ids:
        status = "UNKNOWN"
    publisher = _gateway_event if persist else _event_payload
    return publisher(
        "telegram.response_delivered"
        if status == "ACKNOWLEDGED"
        else "telegram.response_delivery_failed",
        str(command),
        outcome=str(status),
        receipt={
            **identity,
            "delivery_status": status,
            "acknowledged_message_ids": ids if status == "ACKNOWLEDGED" else [],
        },
    )


def handle(event_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
    if event_type == "command-reply:completed":
        _transport_receipt(context)
        return None
    token = _RECEIPT_CONTEXT.set(_receipt_identity(context))
    try:
        result = _handle_command(event_type, context)
        if result is not None and context.get("platform") == "telegram":
            result["observe_delivery"] = True
        return result
    finally:
        _RECEIPT_CONTEXT.reset(token)


def _handle_command(event_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
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
        raw_args, cursor = _split_cursor(str(context.get("raw_args", "")))
        if cursor is not None:
            return {
                "decision": "handled",
                "message": _cursor_page(cursor, command=command, actor=f"telegram:{user_id}"),
            }
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
        # The platform adapter sends this text only after the hook returns.
        # Preparation (including successful formatting) is not a transport ACK.
        message = _format(result, command=command, actor=f"telegram:{user_id}")
        _gateway_event("telegram.response_prepared", command, raw_args=raw_args, result=result)
        return {
            "decision": "handled",
            "message": message,
        }
    except (OSError, RuntimeError, ValueError) as error:
        _gateway_event("engineering.request_failed", command, outcome="REQUEST_FAILED")
        return {
            "decision": "handled",
            "message": _safe_error(f"Engineering Control: {error}"),
        }
