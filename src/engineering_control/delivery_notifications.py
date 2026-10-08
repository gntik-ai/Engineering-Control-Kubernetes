"""NEW-127: Telegram notifications for the backlog delivery lifecycle.

Every producer writes one ``NotificationOutbox`` row (delivered by ``notifier.py`` through
the gateway's ``engineering-alerts`` webhook route, which forwards any topic) under a
deterministic id, so an activity retry or a repeated call never sends twice.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import BacklogIssueRecord, Issue, NotificationOutbox
from engineering_control.projects import PROJECT_SET
from engineering_control.security.redaction import SecretRedactor

LOOP_STATE_TOPIC = "telegram.engineering-backlog-loop-state"
MILESTONE_TOPIC = "telegram.engineering-delivery-milestone"
WRITE_POLICY_TOPIC = "telegram.engineering-write-policy-required"
MESSAGE_LIMIT = 1500
CTL = "/opt/engineering-control/venv/bin/engineering-ctl"

MILESTONE_EVENTS = frozenset(
    {
        "DELIVERY_STARTED",
        "PRS_PUBLISHED",
        "CI_REPAIR_STARTED",
        "PRS_MERGED",
        "STAGING_VERIFIED",
        # NEW-160: a project without a runtime (gntik-ui) is delivered by the merge.
        "MERGE_DELIVERED",
        "DELIVERY_BLOCKED",
        "STAGING_BLOCKED",
        # NEW-123: UI migration flow A milestones.
        "UI_BLUEPRINT_READY",
        "UI_FANOUT_DONE",
    }
)

_WORKFLOW_ID = re.compile(r"[A-Za-z0-9._:-]{1,300}")
_ISSUE = re.compile(r"([A-Za-z0-9._-]{1,100}/)?([A-Za-z0-9._-]{1,100})#([1-9][0-9]{0,8})")
_CODE = re.compile(r"[A-Z][A-Z0-9_:]{0,159}")
_PROJECTS = PROJECT_SET

# One line per known blocker. Prefix families are matched below.
BLOCKER_EXPLANATIONS: dict[str, str] = {
    "PROVIDER_UNAVAILABLE": (
        "a provider call (spec, maker or checker) failed for a transient provider reason "
        "(overloaded, rate limited or unavailable); the ChangeSet is in REQUEST_CHANGES, "
        "so a backlog-retry runs a bounded correction attempt"
    ),
    "CURRENT_REVALIDATION_FLAKY": (
        "the current-source reproduction was flaky: it neither reproduced nor cleared "
        "the issue reliably"
    ),
    "CURRENT_REVALIDATION_REJECTED": "revalidation did not reproduce the issue on current main",
    "CURRENT_REVALIDATION_DUPLICATE": "revalidation found the issue duplicates another finding",
    "CURRENT_REVALIDATION_BLOCKED": "the revalidation itself was blocked",
    "CURRENT_REVALIDATION_FAILED": "the revalidation did not complete",
    "REVALIDATION_FAILED": (
        "the revalidation child failed (provider or infrastructure error); this is not a "
        "verdict on the issue"
    ),
    "CURRENT_SOURCE_REVALIDATION_REQUIRED": "the issue must be re-reproduced on current source",
    "CURRENT_RUNTIME_REVALIDATION_REQUIRED": "the issue must be re-reproduced on the runtime",
    "REVALIDATED_ISSUE_FINDING_MATERIALIZATION_REQUIRED": (
        "the revalidated issue has no confirmed finding yet"
    ),
    "CANONICAL_MIRROR_REFRESH_FAILED": "the canonical repository mirrors could not be refreshed",
    "BACKLOG_RELOAD_FAILED": "the backlog selection could not be reloaded",
    "CURRENT_BACKLOG_APPROVAL_REQUIRED": (
        "the backlog campaign approval is missing or no longer current"
    ),
    "APPROVAL_EXPIRED": "the approval expired",
    "APPROVAL_BINDING_CHANGED": "the approved binding changed after approval",
    "DELIVERY_ISSUE_APPROVAL_REQUIRED_OR_CHANGED": (
        "the per-issue delivery approval is missing or changed"
    ),
    "ISSUE_BODY_CHANGED_SINCE_APPROVAL": (
        "the GitHub issue text changed after approval; amend the approval to the new text"
    ),
    "MAKER_TEST_FAILURE": (
        "a maker test command FAILed and it is not a pre-existing failure on the base commit"
    ),
    "INDEPENDENT_CHECKER_DID_NOT_PASS": "the independent checker did not PASS the change",
    "CORRECTION_LIMIT_EXHAUSTED": "every correction attempt ended with REQUEST_CHANGES",
    "CORRECTION_EXTENSION_USED": (
        "the one operator-granted extra correction attempt also ended with REQUEST_CHANGES"
    ),
    "CHANGESET_VALIDATION_FINDINGS": "deterministic validation found problems in the change",
    "AGENT_CONFIG_CHANGE_REFUSED": (
        "the change adds or modifies agent configuration (.codex/, .claude/ or .mcp.json); "
        "review it and deliver it manually if the issue targets it"
    ),
    "SOURCE_MAKER_DID_NOT_COMPLETE": "the source maker did not complete",
    "SPEC_ARCHITECT_BLOCKED": "the spec architect blocked the change",
    "SOURCE_CHANGES_NOT_COMMITTED": "the maker left uncommitted source changes",
    "DEPLOYMENT_CHANGES_NOT_COMMITTED": "the maker left uncommitted deployment changes",
    "COMPONENT_LEASE_CONFLICT": "another run holds the component lease",
    "PROVIDER_MODEL_AT_CAPACITY": "the model provider is at capacity; retry later",
    "DELIVERY_CHILD_WORKFLOW_FAILED": "the delivery child workflow failed",
    "DELIVERY_NOT_STARTED": "the delivery never started",
    "GITHUB_PUBLICATION_FAILED": "the PRs could not be published",
    "SOURCE_PR_CI_REGRESSION": "the source PR's CI fails checks that pass on main",
    "DEPLOYMENT_PR_CI_REGRESSION": "the deployment PR's CI fails checks that pass on main",
    "SOURCE_PR_CI_FAILED": "the source PR's CI failed",
    # NEW-160: merge-only delivery (projects without a runtime, e.g. gntik-ui).
    "CHANGESET_MISSING": (
        "the PR changes a published package but carries no .changeset/*.md; add one with a "
        "backlog-retry correction or skip the issue"
    ),
    "MERGE_DELIVERY_PROTECTED_PATH_CHANGED": (
        "the PR touches a protected path (CI configuration); Hermes never merges it"
    ),
    "MERGE_DELIVERY_FILE_LISTING_TRUNCATED": (
        "the PR's file list reached GitHub's cap, so the changeset and protected-path "
        "rules cannot be proven"
    ),
    "DEPLOYMENT_PR_CI_FAILED": "the deployment PR's CI failed",
    "SOURCE_PR_CI_TIMEOUT": "the source PR's CI did not finish within its bound",
    "DEPLOYMENT_PR_CI_TIMEOUT": "the deployment PR's CI did not finish within its bound",
    "SOURCE_PR_MERGE_BLOCKED": "the source PR could not be merged within its bound",
    "DEPLOYMENT_PR_MERGE_BLOCKED": "the deployment PR could not be merged within its bound",
    "PROTECTED_MERGE_FAILED": "the protected merge failed",
    "R3_MERGE_REQUIRES_APPROVAL": "an R3 change needs explicit operator approval to merge",
    "CHANGE_SET_STATE_NOT_DELIVERABLE": "the ChangeSet is not in a deliverable state",
    "CHANGE_SET_BINDING_MISSING": "the ChangeSet binding is missing",
    "SOURCE_PR_BINDING_MISSING": "the source PR binding is missing",
    "MAIN_CI_TIMEOUT": "main's CI after the merge did not finish within its bound",
    "STAGING_VERIFICATION_FAILED": "staging workloads did not verify at the merged image",
    "KUBERNETES_WORKLOAD_NOT_READY": "a staging workload is not ready",
    "ENVIRONMENT_UNHEALTHY": "the staging environment is unhealthy",
    "STAGING_GITOPS_CHILD_WORKFLOW_FAILED": "the staging GitOps child workflow failed",
    "STAGING_KUBECONFIG_UNAVAILABLE": "the staging kubeconfig is unavailable",
    "ARGO_SYNC_FAILED": "the Argo CD sync of staging failed",
    "ARGO_SYNC_TIMEOUT": "the Argo CD sync of staging did not converge within its bound",
    "ARGO_REMEDIATION_ROUNDS_EXHAUSTED": "bounded Argo CD remediation rounds are exhausted",
    "ARGO_PRUNE_REQUIRED": "the Argo CD sync needs a prune the flow does not perform",
    "ARGO_OPERATION_BUSY": "another Argo CD operation is running",
    "CI_REPAIR_ROUND_LIMIT_REACHED": "the automatic CI repair rounds are exhausted",
    "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED": "final closure is not enabled",
}

_PREFIX_EXPLANATIONS: tuple[tuple[str, str], ...] = (
    ("CURRENT_REVALIDATION_", "current-source revalidation ended with {rest}"),
    ("LOOP_STEP_FAILED_", "a loop bookkeeping step failed ({rest})"),
    ("CI_REPAIR_", "the automatic CI repair stopped ({rest})"),
    ("ARGO_", "the Argo CD staging sync needs attention ({code})"),
    ("STAGING_GITOPS_", "the staging GitOps flow stopped ({code})"),
    ("SOURCE_PR_", "the source PR needs attention ({code})"),
    ("DEPLOYMENT_PR_", "the deployment PR needs attention ({code})"),
    ("FINAL_", "final closure stopped ({code})"),
)

_CAMPAIGN_APPROVAL_BLOCKERS = frozenset(
    {"CURRENT_BACKLOG_APPROVAL_REQUIRED", "APPROVAL_EXPIRED", "APPROVAL_BINDING_CHANGED"}
)
_SKIP_REASON = "<DUPLICATE|ALREADY_FIXED|SUPERSEDED|NEEDS_PRODUCT_DECISION|NOT_REPRODUCIBLE>"


def bounded_workflow_id(value: Any) -> str | None:
    text = str(value or "")
    return text if _WORKFLOW_ID.fullmatch(text) else None


def bounded_code(value: Any) -> str | None:
    text = str(value or "")
    return text if _CODE.fullmatch(text) else None


def parse_issue(value: Any) -> tuple[str, int] | None:
    """``owner/repo#n`` or ``repo#n`` to (repository, number)."""
    match = _ISSUE.fullmatch(str(value or ""))
    if match is None:
        return None
    return f"{match.group(1) or ''}{match.group(2)}", int(match.group(3))


def short_repository(repository: str) -> str:
    return repository.rsplit("/", 1)[-1]


def loop_workflow_of(child_workflow_id: str) -> str:
    """Child workflow ids are ``<loop id>-issue-<ordinal>...``."""
    head, separator, _ = child_workflow_id.partition("-issue-")
    return head if separator else child_workflow_id


def explain_blocker(code: str | None) -> str:
    if not code:
        return "no blocker was recorded"
    inner = code
    prefix = ""
    if code.startswith("DELIVERY_FAILED:"):
        inner = code.split(":", 1)[1]
        prefix = "the delivery child failed: "
    if inner in BLOCKER_EXPLANATIONS:
        return prefix + BLOCKER_EXPLANATIONS[inner]
    for marker, template in _PREFIX_EXPLANATIONS:
        if inner.startswith(marker):
            return prefix + template.format(rest=inner[len(marker) :], code=inner)
    if prefix:
        return f"the delivery child failed ({inner})"
    return f"blocked by {inner}; see /loop status for details"


def loop_commands(
    *,
    workflow_id: str,
    project: str | None,
    issue: str | None,
    state: str,
    blocker: str | None,
    control_state: str | None = None,
    campaign_id: str | None = None,
) -> list[str]:
    """The exact operator commands that resolve one loop state."""
    commands = [f"/loop status {workflow_id}"]
    if state != "PAUSED":
        return commands
    if control_state == "PAUSED_AFTER_CURRENT" or not blocker:
        return [*commands, f"/loop resume {workflow_id}", f"{CTL} backlog-resume {workflow_id}"]
    parsed = parse_issue(issue)
    repository = short_repository(parsed[0]) if parsed else "<repository>"
    number = str(parsed[1]) if parsed else "<issue-number>"
    target = f"{workflow_id} {repository} {number}"
    code = blocker.split(":", 1)[1] if blocker.startswith("DELIVERY_FAILED:") else blocker
    if code == "ISSUE_BODY_CHANGED_SINCE_APPROVAL":
        amend = f"/loop amend {project or '<project>'} {repository} {number}"
        return [*commands, amend, f"{amend} <sha-prefix from the preview> retry"]
    if code in _CAMPAIGN_APPROVAL_BLOCKERS:
        campaign = campaign_id or "<backlog campaign id>"
        commands.extend(
            [
                f"{CTL} backlog-approval-candidate {project or '<project>'} {campaign} "
                "full-delivery R2 --max-items 1",
                f"{CTL} backlog-approve {project or '<project>'} {campaign} <approval_binding> "
                "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP",
            ]
        )
    commands.extend(
        [
            f"/loop retry {target}",
            f"/loop skip {target} {_SKIP_REASON}",
            f"{CTL} backlog-retry {target}",
        ]
    )
    return commands


_CODE_SPAN = re.compile(r"(`[^`]*`)")


def trim_spans(text: str, limit: int) -> str:
    """Shorten to at most `limit` characters cutting only BETWEEN code spans, never inside
    one (NEW-127 review 3): untrusted text stays inside its span or is dropped whole."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    kept = ""
    for part in _CODE_SPAN.split(text):
        if not part:
            continue
        if len(kept) + len(part) > limit - 3:
            if not part.startswith("`"):
                kept += part[: max(0, limit - 3 - len(kept))]
            break
        kept += part
    return kept.rstrip() + "..."


def bounded_message(message: str) -> str:
    return trim_spans(message, MESSAGE_LIMIT)


# NEW-127 review 3: the gateway renders the whole JSON payload and truncates it (4,000
# characters), so a code span must never straddle that cut: the payload is bounded against
# a worst-case rendering (ASCII-escaped, indented) well below the gateway's limit, and each
# string field's spans are checked after any truncation.
PAYLOAD_RENDER_LIMIT = 3000


def _guarded(value: Any) -> Any:
    """Every string keeps its code spans; a string whose spans no longer pair (after any
    truncation) becomes plain text. Collections are guarded element by element."""
    if isinstance(value, str):
        return inert_message(trim_spans(value, 300))
    if isinstance(value, list):
        return [_guarded(item) for item in value]
    if isinstance(value, dict):
        return {key: _guarded(item) for key, item in value.items()}
    return value


_WHOLE_KEYS = frozenset({"commands"})


def _whole(value: Any) -> Any:
    """Paired code spans are still required, but strings are never shortened."""
    if isinstance(value, str):
        return inert_message(value)
    if isinstance(value, list):
        return [_whole(item) for item in value]
    return _guarded(value)


def _rendered_size(payload: dict[str, Any]) -> int:
    return len(json.dumps({"topic": "telegram.x", "payload": payload}, indent=2, ensure_ascii=True))


def bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Every field's code spans pair, and the whole payload fits the gateway's rendering,
    so the gateway's own truncation can never cut a span open."""
    message = str(payload.get("message") or "")
    bounded = {
        # Generated operator commands are never cut (a partial command is wrong): they stay
        # whole, or the whole list is dropped below if the budget requires it.
        key: (_whole(value) if key in _WHOLE_KEYS else _guarded(value))
        for key, value in payload.items()
        if key != "message"
    }
    bounded["message"] = inert_message(bounded_message(message))
    # Drop optional collections first (they only repeat what the message says).
    for key in sorted(
        (key for key, value in bounded.items() if isinstance(value, (list, dict))),
        key=lambda key: -len(json.dumps(bounded[key], ensure_ascii=True)),
    ):
        if _rendered_size(bounded) <= PAYLOAD_RENDER_LIMIT:
            break
        bounded.pop(key)
    limit = MESSAGE_LIMIT
    while _rendered_size(bounded) > PAYLOAD_RENDER_LIMIT and limit > 200:
        limit = int(limit * 0.8)
        bounded["message"] = inert_message(trim_spans(message, limit))
    if _rendered_size(bounded) > PAYLOAD_RENDER_LIMIT:
        # Last resort: the message alone, as plain text without any formatting characters.
        bounded = {"message": " ".join(_MARKDOWN_CONTROL.sub(" ", message).split())[:1000]}
    return bounded


def enqueue(
    session: Session,
    *,
    topic: str,
    dedupe_key: str,
    payload: dict[str, Any],
    redactor: SecretRedactor | None = None,
) -> bool:
    """Add one PENDING row under a deterministic id; False when it already exists."""
    identity = uuid5(NAMESPACE_URL, f"notification:{topic}:{dedupe_key}")
    if session.get(NotificationOutbox, identity) is not None:
        return False
    safe = (redactor or SecretRedactor()).redact(payload).value
    if not isinstance(safe, dict):
        raise TypeError("redacted notification payload must remain an object")
    safe = bounded_payload(safe)
    now = datetime.now(UTC)
    session.add(
        NotificationOutbox(
            id=identity,
            topic=topic,
            status="PENDING",
            payload_json=safe,
            attempts=0,
            next_attempt_at=now,
            delivered_at=None,
            last_error_code=None,
        )
    )
    return True


def orchestration_sequence(payload: dict[str, Any]) -> tuple[str, str] | None:
    """(workflow id, run:activity) of the executing activity; stable across retries."""
    value = payload.get("orchestration")
    if not isinstance(value, dict):
        return None
    workflow_id = bounded_workflow_id(value.get("workflow_id"))
    run_id = str(value.get("workflow_run_id") or "")
    activity_id = str(value.get("activity_id") or "")
    if workflow_id is None or not run_id or not activity_id:
        return None
    return workflow_id, f"{run_id[:64]}:{activity_id[:64]}"


def loop_state_label(status: str, previous: str | None) -> str | None:
    """The notified transition, or None when the state did not change."""
    if status == "RUNNING":
        return "RESUMED" if previous in {"PAUSED", "BLOCKED"} else None
    if status == "PAUSED":
        return "PAUSED"
    if status in {"COMPLETE", "FAILED", "CANCELLED", "STOPPED"}:
        return status if previous != status else None
    return None


def enqueue_loop_state(
    session: Session,
    payload: dict[str, Any],
    *,
    status: str,
    previous: str | None,
    redactor: SecretRedactor | None = None,
) -> bool:
    """One notification per loop-state transition (NEW-127)."""
    label = loop_state_label(status, previous)
    if label is None:
        return False
    sequence = orchestration_sequence(payload)
    workflow_id = bounded_workflow_id(payload.get("workflow_id")) or (
        sequence[0] if sequence else None
    )
    if workflow_id is None:
        return False
    project = str(payload.get("project") or "")
    project = project if project in _PROJECTS else ""
    issue = str(payload.get("issue") or "")
    if parse_issue(issue) is None:
        target_repository = payload.get("target_repository")
        target_number = payload.get("target_issue_number")
        issue = (
            f"{target_repository}#{target_number}"
            if target_repository and isinstance(target_number, int)
            else ""
        )
        if parse_issue(issue) is None:
            issue = ""
    blocker = bounded_code(payload.get("blocker")) if status == "PAUSED" else None
    control_state = bounded_code(payload.get("control_state"))
    campaign_id = str(payload.get("backlog_campaign_id") or "")
    campaign_id = campaign_id if re.fullmatch(r"[0-9a-f-]{36}", campaign_id) else ""
    if sequence is not None and sequence[0] == workflow_id:
        dedupe = f"{workflow_id}:{sequence[1]}"
    else:
        # Outside an activity: only real state changes notify, keyed by the change.
        if previous == status:
            return False
        dedupe = f"{workflow_id}:{previous}>{status}:{blocker or control_state or ''}"
    if status == "PAUSED" and control_state == "PAUSED_AFTER_CURRENT":
        explanation = "paused after the current issue at the operator's request"
    elif status == "PAUSED":
        explanation = explain_blocker(blocker)
    elif label == "RESUMED":
        explanation = "the loop resumed after a pause"
    elif status == "COMPLETE":
        explanation = "the loop completed"
    elif status == "FAILED":
        explanation = "the loop failed; it no longer runs"
    elif status == "STOPPED":
        explanation = "the loop stopped after the current issue"
    else:
        explanation = "the loop was cancelled"
    commands = loop_commands(
        workflow_id=workflow_id,
        project=project or None,
        issue=issue or None,
        state=status,
        blocker=blocker,
        control_state=control_state,
        campaign_id=campaign_id or None,
    )
    message = (
        f"Hermes backlog loop {label}"
        + (f" for {project}" if project else "")
        + (f" {issue}" if issue else "")
        + f" (loop {workflow_id})"
        + (f": {blocker}" if blocker else "")
        + f" - {explanation}."
    )
    return enqueue(
        session,
        topic=LOOP_STATE_TOPIC,
        dedupe_key=dedupe,
        payload={
            "message": message,
            "event": label,
            "loop_state": status,
            "project": project or None,
            "issue": issue or None,
            "workflow_id": workflow_id,
            "blocker": blocker,
            "control_state": control_state,
            "backlog_campaign_id": campaign_id or None,
            "commands": commands,
        },
        redactor=redactor,
    )


def issue_for_finding(session: Session, finding_id: Any, campaign_id: Any = None) -> str:
    """``owner/repo#n`` (or "") for a finding, from the backlog record first."""
    try:
        finding = UUID(str(finding_id))
    except ValueError:
        return ""
    query = select(BacklogIssueRecord).where(BacklogIssueRecord.finding_id == finding)
    if campaign_id is not None:
        try:
            query = query.where(BacklogIssueRecord.backlog_campaign_id == UUID(str(campaign_id)))
        except ValueError:
            pass
    record = session.scalars(query.order_by(BacklogIssueRecord.updated_at.desc()).limit(1)).first()
    if record is not None:
        return f"{record.repository}#{record.issue_number}"
    issue = session.scalar(select(Issue).where(Issue.finding_id == finding))
    if issue is not None and issue.number:
        return f"{issue.repository}#{issue.number}"
    return ""


def enqueue_milestone(
    session: Session,
    *,
    event: str,
    dedupe_key: str,
    project: str | None,
    issue: str | None,
    workflow_id: str | None,
    detail: str,
    extra: dict[str, Any] | None = None,
    redactor: SecretRedactor | None = None,
) -> bool:
    """One per-issue delivery milestone (NEW-127)."""
    if event not in MILESTONE_EVENTS:
        raise ValueError("delivery milestone event is not allowlisted")
    project = project if project in _PROJECTS else None
    issue = issue if parse_issue(issue) else None
    workflow_id = bounded_workflow_id(workflow_id)
    loop_id = loop_workflow_of(workflow_id) if workflow_id else None
    titles = {
        "DELIVERY_STARTED": "delivery started",
        "PRS_PUBLISHED": "PRs published",
        "CI_REPAIR_STARTED": "CI repair started",
        "PRS_MERGED": "PRs merged",
        "STAGING_VERIFIED": "STAGING_VERIFIED",
        "MERGE_DELIVERED": "MERGE_DELIVERED",
        "DELIVERY_BLOCKED": "delivery BLOCKED",
        "STAGING_BLOCKED": "staging delivery BLOCKED",
        "UI_BLUEPRINT_READY": "UI blueprint ready for approval",
        "UI_FANOUT_DONE": "UI fan-out done",
    }
    message = (
        f"Hermes {titles[event]}"
        + (f" for {project}" if project else "")
        + (f" {issue}" if issue else "")
        + (f" (loop {loop_id})" if loop_id else "")
        + f": {detail}"
    )
    commands = [f"/loop status {loop_id}"] if loop_id else []
    return enqueue(
        session,
        topic=MILESTONE_TOPIC,
        dedupe_key=f"{event}:{dedupe_key}",
        payload={
            "message": message,
            "event": event,
            "project": project,
            "issue": issue,
            "workflow_id": workflow_id,
            "loop_workflow_id": loop_id,
            **(extra or {}),
            **({"commands": commands} if commands else {}),
        },
        redactor=redactor,
    )


def attempt_label(child_workflow_id: str) -> str:
    """Human attempt label from ``...-issue-<n>[-retry-<r>|-ci-repair-<r>]-attempt-<a>``."""
    attempt = re.search(r"-attempt-([0-9]+)$", child_workflow_id)
    retry = re.search(r"-retry-([0-9]+)", child_workflow_id)
    repair = re.search(r"-ci-repair-([0-9]+)", child_workflow_id)
    label = f"attempt {attempt.group(1) if attempt else 1}"
    if repair:
        label += f" of CI repair round {repair.group(1)}"
    elif retry:
        label += f" of operator retry {retry.group(1)}"
    return label


def literal(value: Any, limit: int = 160) -> str:
    """Untrusted text as one inline-code span (NEW-127 review).

    The gateway renders the notification through Telegram's Markdown-to-MarkdownV2
    converter, which keeps inline code verbatim: links, bold or italics inside it are
    never formatted, and Telegram does not linkify URLs in code. A backtick (the only way
    out of the span) and control characters are replaced first.
    """
    # NEW-127 review 2: redact BEFORE wrapping, so a redaction pattern (e.g. "cookie: …")
    # can never consume the closing backtick; the span is space-padded for the same reason.
    text = str(SecretRedactor().redact_text(str(value)).value)
    text = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in text)
    text = " ".join(text.replace("`", "'").split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return f"` {text or '-'} `"


_MARKDOWN_CONTROL = re.compile(r"[`*_\[\]()~|>#]")


def inert_message(message: str) -> str:
    """Last guard: a message whose code spans no longer pair is sent as plain text."""
    if message.count("`") % 2 == 0:
        return message
    return " ".join(_MARKDOWN_CONTROL.sub(" ", message).split())


def maker_failures(context: dict[str, Any]) -> list[str]:
    """Failing maker test commands, each as a literal."""
    failed: list[str] = []
    for key in ("source_maker_result", "deployment_maker_result"):
        maker = context.get(key)
        structured = maker.get("structured_result") if isinstance(maker, dict) else None
        tests = structured.get("tests") if isinstance(structured, dict) else None
        if not isinstance(tests, list):
            continue
        failed.extend(
            literal(test.get("command", ""))
            for test in tests
            if isinstance(test, dict) and test.get("status") == "FAIL"
        )
    return failed[:5]


def checker_verdict(context: dict[str, Any]) -> str | None:
    checker = context.get("agent_result")
    structured = checker.get("structured_result") if isinstance(checker, dict) else None
    if not isinstance(structured, dict) or "decision" not in structured:
        return None
    decision = bounded_code(structured.get("decision")) or literal(structured.get("decision"), 40)
    summary = str(structured.get("summary") or "").strip()
    return decision + (f" {literal(summary, 300)}" if summary else "")


def enqueue_blocked_start(
    session: Session,
    payload: dict[str, Any],
    *,
    workflow_id: str,
    blocker: Any,
    redactor: SecretRedactor | None = None,
) -> bool:
    """A loop refused at its initial selection (NEW-97 closed Run) is announced once."""
    code = bounded_code(blocker) or "BLOCKED"
    project = str(payload.get("project") or "")
    project = project if project in _PROJECTS else ""
    target_repository = payload.get("target_repository")
    target_number = payload.get("target_issue_number")
    issue = f"{target_repository}#{target_number}" if target_repository and target_number else ""
    issue = issue if parse_issue(issue) else ""
    campaign_id = str(payload.get("backlog_campaign_id") or "")
    campaign_id = campaign_id if re.fullmatch(r"[0-9a-f-]{36}", campaign_id) else ""
    commands = [
        line
        for line in loop_commands(
            workflow_id=workflow_id,
            project=project or None,
            issue=issue or None,
            state="PAUSED",
            blocker=code,
            campaign_id=campaign_id or None,
        )
        # The loop has ended: retry/skip signals cannot reach it.
        if not line.startswith(("/loop retry", "/loop skip", f"{CTL} backlog-retry"))
    ]
    message = (
        "Hermes backlog loop BLOCKED at start"
        + (f" for {project}" if project else "")
        + (f" {issue}" if issue else "")
        + f" (loop {workflow_id}): {code} - {explain_blocker(code)}. The loop did not start;"
        " resolve the cause and start it again."
    )
    return enqueue(
        session,
        topic=LOOP_STATE_TOPIC,
        dedupe_key=f"{workflow_id}:blocked-start",
        payload={
            "message": message,
            "event": "BLOCKED_AT_START",
            "loop_state": "STOPPED",
            "project": project or None,
            "issue": issue or None,
            "workflow_id": workflow_id,
            "blocker": code,
            "backlog_campaign_id": campaign_id or None,
            "commands": commands,
        },
        redactor=redactor,
    )
