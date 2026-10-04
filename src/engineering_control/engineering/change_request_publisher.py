"""NEW-108: the operator publishes an approved change request with their own gh login.

Hermes never writes the issue. `engineering-ctl change-publish` fetches the APPROVED
request, re-derives its exact title, body and binding locally, adopts an issue that
already carries exactly this publication, or creates it with the operator's `gh`, and then
records the URL through the bound /published endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess  # noqa: S404 - fixed argument vectors only, never a shell
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from engineering_control.adapters.github.client import ALLOWED_REPOSITORIES
from engineering_control.engineering.backlog import CHANGE_REQUEST_LABEL
from engineering_control.engineering.change_requests import (
    MARKER_PATTERN,
    UI_KIT_REPOSITORIES,
    ChangeReviewRecord,
    UiChangeContext,
    approval_binding,
    approval_record_matches,
    render_change_request,
    target_repository_allowed,
    validate_proposal,
)

GhRunner = Callable[[list[str]], str]
_RECENT_ISSUES = 30


class ChangePublishError(ValueError):
    """The change request cannot be published as approved."""


class OperatorAPI(Protocol):
    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass(frozen=True)
class PublicationPlan:
    change_request_id: str
    revision: int
    decision_sequence: int
    approval_binding: str
    repository: str
    title: str
    body: str
    marker: str
    wanted_labels: tuple[str, ...]


def run_gh(arguments: list[str]) -> str:
    """Run the operator's gh with a fixed argument vector (never a shell)."""
    executable = shutil.which("gh")
    if executable is None:
        raise ChangePublishError("the operator's gh CLI is not installed")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed vector, shell=False
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ChangePublishError(f"gh {' '.join(arguments[:2])} could not run") from error
    if completed.returncode != 0:
        raise ChangePublishError(
            f"gh {' '.join(arguments[:2])} failed: {completed.stderr.strip()[:500]}"
        )
    return completed.stdout


def verified_plan(view: dict[str, Any]) -> PublicationPlan:
    """Re-derive the approved issue locally; refuse anything that does not match."""
    if view.get("status") != "APPROVED":
        raise ChangePublishError("only an APPROVED change request can be published")
    change_request_id = str(view.get("id", ""))
    project = str(view.get("project", ""))
    repository = str(view.get("target_repository", ""))
    # NEW-123: a UI-kit repository (gntik-ui) is published only by the operator's gh.
    known = ALLOWED_REPOSITORIES | frozenset(UI_KIT_REPOSITORIES.get(project, ()))
    if repository not in known or not target_repository_allowed(project, repository):
        raise ChangePublishError("change request repository is not allowlisted")
    revision = view.get("revision")
    sequence = view.get("decision_sequence")
    binding = str(view.get("approval_binding") or "")
    title = str(view.get("issue_title") or "")
    body = str(view.get("issue_body") or "")
    fingerprint = str(view.get("fingerprint") or "")
    if not isinstance(revision, int) or not isinstance(sequence, int) or sequence < 1:
        raise ChangePublishError("change request decision record is incomplete")
    try:
        proposal = validate_proposal(view.get("proposal"), project, repository)
        raw_context = view.get("ui_context")
        ui_context = UiChangeContext.model_validate(raw_context) if raw_context else None
        rendered = render_change_request(
            change_request_id=change_request_id,
            revision=revision,
            project=project,
            target_repository=repository,
            proposal=proposal,
            review=ChangeReviewRecord.model_validate(view.get("review")),
            description_redacted=str(view.get("description_redacted", "")),
            ui_context=ui_context,
        )
    except ValueError as error:
        raise ChangePublishError("change request content cannot be re-rendered") from error
    suffix = f"\n{rendered.marker}\n"
    body_core = body.removesuffix(suffix) if body.endswith(suffix) else None
    local_binding = (
        approval_binding(
            change_request_id=change_request_id,
            revision=revision,
            fingerprint=fingerprint,
            target_repository=repository,
            title=title,
            body_sha256=hashlib.sha256(body_core.encode("utf-8")).hexdigest(),
        )
        if body_core is not None
        else ""
    )
    if (
        local_binding != binding
        or rendered.approval_binding != binding
        or rendered.fingerprint != fingerprint
        or rendered.title != title
        or rendered.body != body
    ):
        raise ChangePublishError("change request title, body or repository changed since approval")
    # The same approval-record check /published applies, before any GitHub write.
    if not approval_record_matches(
        view.get("approval_snapshot"),
        project=project,
        target_repository=repository,
        proposal=view.get("proposal"),
        review=view.get("review"),
        description_redacted=str(view.get("description_redacted", "")),
        issue_title=title,
        issue_body=body,
        fingerprint=fingerprint,
        revision=revision,
        decision_sequence=sequence,
        decided_by=view.get("decided_by"),
        decided_at=view.get("decided_at"),
        approval_binding=binding,
    ):
        raise ChangePublishError("change request no longer matches its approval record")
    return PublicationPlan(
        change_request_id=change_request_id,
        revision=revision,
        decision_sequence=sequence,
        approval_binding=binding,
        repository=repository,
        title=title,
        body=body,
        marker=rendered.marker,
        wanted_labels=tuple(
            dict.fromkeys(
                (CHANGE_REQUEST_LABEL, proposal.kind, *(ui_context.labels if ui_context else ()))
            )
        ),
    )


def create_command(plan: PublicationPlan, body_file: str, labels: tuple[str, ...]) -> list[str]:
    command = [
        "issue",
        "create",
        "--repo",
        plan.repository,
        "--title",
        plan.title,
        "--body-file",
        body_file,
    ]
    for label in labels:
        command.extend(("--label", label))
    return command


def publish_change_request(
    client: OperatorAPI,
    change_request_id: str,
    *,
    dry_run: bool = False,
    gh: GhRunner = run_gh,
) -> dict[str, Any]:
    view = client.request("GET", f"/v1/change-requests/{change_request_id}")
    if not isinstance(view, dict):
        raise ChangePublishError("change request response is invalid")
    plan = verified_plan(view)
    if dry_run:
        command = create_command(plan, "<body-file>", ())
        return {
            "status": "DRY_RUN",
            "change_request_id": plan.change_request_id,
            "repository": plan.repository,
            "title": plan.title,
            "body": plan.body,
            "gh_command": shlex.join(["gh", *command]),
            "labels_if_they_exist": list(plan.wanted_labels),
        }
    adopted = _existing_publication(plan, gh)
    labels: tuple[str, ...] = ()
    if adopted is None:
        labels = _existing_labels(plan, gh)
        with tempfile.TemporaryDirectory(prefix="change-publish-") as directory:
            body_file = Path(directory) / "body.md"
            body_file.write_text(plan.body, encoding="utf-8")
            body_file.chmod(0o600)
            url = _issue_url(plan, gh(create_command(plan, str(body_file), labels)).strip())
    else:
        url = adopted
    recorded = client.request(
        "POST",
        f"/v1/change-requests/{plan.change_request_id}/published",
        {
            "issue_url": url,
            "approval_binding": plan.approval_binding,
            "decision_sequence": plan.decision_sequence,
        },
    )
    return {
        **(recorded if isinstance(recorded, dict) else {}),
        "adopted": adopted is not None,
        "labels_applied": list(labels),
    }


def _existing_publication(plan: PublicationPlan, gh: GhRunner) -> str | None:
    """The issue that is exactly this publication; any other claimant is a conflict."""
    fields = "number,title,body,url,author"
    found: dict[int, dict[str, Any]] = {}
    for arguments in (
        ["--search", plan.change_request_id, "--limit", "100"],
        ["--limit", str(_RECENT_ISSUES)],
    ):
        raw = gh(
            ["issue", "list", "--repo", plan.repository, "--state", "all", "--json", fields]
            + arguments
        )
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ChangePublishError("gh issue list returned invalid JSON") from error
        if not isinstance(items, list):
            raise ChangePublishError("gh issue list returned invalid data")
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("number"), int):
                found[item["number"]] = item
    adopted: str | None = None
    for number in sorted(found):
        item = found[number]
        raw_body = item.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        claims = [
            match for match in MARKER_PATTERN.findall(body) if match[0] == plan.change_request_id
        ]
        if not claims:
            continue
        normalized = body.replace("\r\n", "\n").rstrip("\n")
        if (
            item.get("title") != plan.title
            or normalized != plan.body.rstrip("\n")
            or normalized.splitlines()[-1] != plan.marker
        ):
            raise ChangePublishError(
                f"issue #{number} claims this change request but is not its approved "
                "publication; reconcile it on GitHub first"
            )
        url = _issue_url(plan, str(item.get("url", "")))
        if adopted is not None and adopted != url:
            raise ChangePublishError("several issues carry this change request")
        adopted = url
    return adopted


def _existing_labels(plan: PublicationPlan, gh: GhRunner) -> tuple[str, ...]:
    """Only labels the repository already has; labels are never created."""
    try:
        items = json.loads(
            gh(["label", "list", "--repo", plan.repository, "--json", "name", "--limit", "500"])
        )
    except (ChangePublishError, json.JSONDecodeError):
        return ()
    names = {
        str(item["name"])
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    return tuple(label for label in plan.wanted_labels if label in names)


def _issue_url(plan: PublicationPlan, value: str) -> str:
    if not re.fullmatch(
        rf"https://github\.com/{re.escape(plan.repository)}/issues/[1-9][0-9]*", value
    ):
        raise ChangePublishError("gh did not return an issue URL in the approved repository")
    return value
