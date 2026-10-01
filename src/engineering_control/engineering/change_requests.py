"""Operator change requests (NEW-108): intake, proposal contract, rendering and binding.

An operator describes a change in free text; Hermes drafts a proposal (Claude), an
independent provider reviews it (Codex), and the operator approves exactly one rendered
GitHub issue. Everything here is pure and deterministic so the API, the controller
activities and the narrow issue-create policy all recompute the same title, body,
fingerprint and approval binding from the same stored content.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engineering_control.adapters.github.client import ALLOWED_REPOSITORIES
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    canonical_sha256,
)
from engineering_control.security.redaction import SecretRedactor

CHANGE_REQUEST_STATUSES = ("DRAFTING", "CANDIDATE", "APPROVED", "PUBLISHED", "REJECTED", "FAILED")
DESCRIPTION_MAX_BYTES = 8 * 1024
DESCRIPTION_MIN_CHARACTERS = 20
ISSUE_TITLE_PREFIX = "Change request: "
MAXIMUM_ISSUE_BODY_CHARACTERS = 60_000
CHANGE_REQUEST_KINDS = ("feature", "enhancement", "bug", "chore")
REVIEW_CATEGORIES = ("code_claim", "testability", "scope", "risk", "other")

_REPOSITORY_PATTERN = r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
# The GitHub broker accepts only this character set in an issue title (GhCliBroker._value).
_TITLE_UNSAFE = re.compile(r"[^A-Za-z0-9._:/@+ =,\"'-]+")
_BINDING = re.compile(r"^[a-f0-9]{64}$")
# Any revision's hidden marker for a change request (group 1 is the request id).
MARKER_PATTERN = re.compile(
    r"<!-- hermes-change-request id=([0-9a-f-]{36}) revision=([0-9]+) "
    r"fingerprint=(sha256:[a-f0-9]{64}) binding=([a-f0-9]{64}) -->"
)


class ChangeRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ProposalEntry = Annotated[str, Field(min_length=1, max_length=600)]


class AffectedRepository(ChangeRequestModel):
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    role: Literal["source", "deployment"]


class ChangeProposal(ChangeRequestModel):
    """The drafted OpenSpec-style proposal; the fingerprint covers exactly these fields."""

    title: str = Field(min_length=1, max_length=100)
    kind: Literal["feature", "enhancement", "bug", "chore"]
    why: str = Field(min_length=1, max_length=3000)
    what_changes: tuple[ProposalEntry, ...] = Field(min_length=1, max_length=15)
    out_of_scope: tuple[ProposalEntry, ...] = Field(max_length=15)
    affected_repositories: tuple[AffectedRepository, ...] = Field(min_length=1, max_length=2)
    acceptance_criteria: tuple[ProposalEntry, ...] = Field(min_length=1, max_length=15)
    risks: tuple[ProposalEntry, ...] = Field(max_length=15)
    rollout: tuple[ProposalEntry, ...] = Field(max_length=15)
    kill_switch: tuple[ProposalEntry, ...] = Field(max_length=15)
    open_questions: tuple[ProposalEntry, ...] = Field(max_length=15)
    proposed_risk: Literal["R0", "R1", "R2", "R3"]
    risk_rationale: str = Field(min_length=1, max_length=1500)

    @model_validator(mode="after")
    def unique_repositories(self) -> ChangeProposal:
        identities = [(item.repository, item.role) for item in self.affected_repositories]
        if len(identities) != len(set(identities)):
            raise ValueError("affected repositories must be unique")
        return self


class ChangeReviewFinding(ChangeRequestModel):
    category: Literal["code_claim", "testability", "scope", "risk", "other"]
    detail: str = Field(min_length=1, max_length=1000)
    reference: str = Field(max_length=300)


class ChangeReview(ChangeRequestModel):
    """One independent review round of a proposal."""

    decision: Literal["APPROVE", "REVISE"]
    summary: str = Field(min_length=1, max_length=2000)
    findings: tuple[ChangeReviewFinding, ...] = Field(max_length=30)


class ChangeReviewRecord(ChangeRequestModel):
    """The stored outcome of at most two review rounds; never an approval."""

    provider: Literal["codex"] = "codex"
    decision: Literal["APPROVE", "REVISE"]
    summary: str = Field(min_length=1, max_length=2000)
    rounds: int = Field(ge=1, le=2)
    unresolved_findings: tuple[ChangeReviewFinding, ...] = Field(max_length=30)
    history: tuple[ChangeReview, ...] = Field(min_length=1, max_length=2)

    @classmethod
    def from_rounds(cls, rounds: list[ChangeReview]) -> ChangeReviewRecord:
        if not 1 <= len(rounds) <= 2:
            raise ValueError("a change request review has one or two rounds")
        final = rounds[-1]
        return cls(
            decision=final.decision,
            summary=final.summary,
            rounds=len(rounds),
            unresolved_findings=final.findings if final.decision == "REVISE" else (),
            history=tuple(rounds),
        )


class ChangeRequestIntake(ChangeRequestModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    description_redacted: str = Field(min_length=DESCRIPTION_MIN_CHARACTERS)
    description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    redacted: bool
    target_repository: str = Field(pattern=_REPOSITORY_PATTERN)


class RenderedChangeRequest(ChangeRequestModel):
    title: str
    body: str
    body_sha256: str
    fingerprint: str
    approval_binding: str
    marker: str


def project_repositories(project: str) -> tuple[str, str]:
    """The project's allowlisted (source, deployment) repositories."""
    if project not in BACKLOG_PROJECT_PAIRS:
        raise ValueError("unknown project")
    return BACKLOG_PROJECT_PAIRS[project]


def target_repository_allowed(project: str, repository: str) -> bool:
    return (
        project in BACKLOG_PROJECT_PAIRS
        and repository in BACKLOG_PROJECT_PAIRS[project]
        and repository in ALLOWED_REPOSITORIES
    )


def prepare_intake(
    project: str, description: str, target_repository: str | None = None
) -> ChangeRequestIntake:
    """Bound, redact and bind one operator description before anything stores it."""
    if project not in BACKLOG_PROJECT_PAIRS:
        raise ValueError("unknown project")
    if len(description.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        raise ValueError("change description exceeds 8 KiB of UTF-8")
    if len(description.strip()) < DESCRIPTION_MIN_CHARACTERS:
        raise ValueError(
            f"change description must be at least {DESCRIPTION_MIN_CHARACTERS} characters"
        )
    repository = target_repository or project_repositories(project)[0]
    if not target_repository_allowed(project, repository):
        raise ValueError("target repository is not allowlisted for the project")
    report = SecretRedactor().redact_text(description.strip())
    redacted_text = str(report.value)
    if len(redacted_text.strip()) < DESCRIPTION_MIN_CHARACTERS:
        raise ValueError("change description is too short after credential redaction")
    return ChangeRequestIntake(
        project=project,  # type: ignore[arg-type]
        description_redacted=redacted_text,
        description_sha256=hashlib.sha256(redacted_text.encode("utf-8")).hexdigest(),
        redacted=report.replacements > 0,
        target_repository=repository,
    )


def validate_proposal(value: Any, project: str) -> ChangeProposal:
    """Validate a drafted proposal and bind its repositories to the project allowlist."""
    proposal = ChangeProposal.model_validate(value)
    source, deployment = project_repositories(project)
    allowed = {(source, "source"), (deployment, "deployment")}
    for item in proposal.affected_repositories:
        if (item.repository, item.role) not in allowed:
            raise ValueError("proposal names a repository outside the project allowlist")
    return proposal


def proposal_fingerprint(proposal: ChangeProposal) -> str:
    return f"sha256:{canonical_sha256(proposal.model_dump(mode='json'))}"


def issue_title(proposal: ChangeProposal) -> str:
    safe = " ".join(_TITLE_UNSAFE.sub(" ", proposal.title).split())[:100].strip()
    return f"{ISSUE_TITLE_PREFIX}{safe or 'untitled'}"


def approval_binding(
    *,
    change_request_id: str,
    revision: int,
    fingerprint: str,
    target_repository: str,
    title: str,
    body_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "change_request_id": change_request_id,
            "revision": revision,
            "fingerprint": fingerprint,
            "target_repository": target_repository,
            "title": title,
            "body_sha256": body_sha256,
        }
    )


def approval_phrase(project: str, binding: str) -> str:
    if not _BINDING.fullmatch(binding):
        raise ValueError("approval binding is invalid")
    return f"APPROVE_CHANGE_{project.upper()}_{binding[:16].upper()}"


def change_request_workflow_id(change_request_id: str, revision: int) -> str:
    return f"change-request-{change_request_id}-r{revision}"


def change_request_marker(
    *, change_request_id: str, revision: int, fingerprint: str, binding: str
) -> str:
    return (
        f"<!-- hermes-change-request id={change_request_id} revision={revision} "
        f"fingerprint={fingerprint} binding={binding} -->"
    )


def render_change_request(
    *,
    change_request_id: str,
    revision: int,
    project: str,
    target_repository: str,
    proposal: ChangeProposal,
    review: ChangeReviewRecord,
    description_redacted: str,
) -> RenderedChangeRequest:
    """Render the exact issue title and body an operator approves (pure, deterministic).

    The approval binding covers the body without its trailing hidden marker, because the
    marker itself carries the binding; the marker is then appended verbatim.
    """
    fingerprint = proposal_fingerprint(proposal)
    title = issue_title(proposal)
    # kind/Risk lines stay first: backlog import reads them from a bounded body excerpt.
    lines = [
        f"kind: {proposal.kind}",
        f"Risk: {proposal.proposed_risk}",
        "",
        "## Change request",
        "",
        f"- Request: `{change_request_id}` revision {revision}",
        f"- Project: `{project}`",
        f"- Target repository: `{target_repository}`",
        f"- Proposal fingerprint: `{fingerprint}`",
        "",
        "## Why",
        "",
        f"- {_line(proposal.why)}",
        "",
        "## What changes",
        "",
        *_bullets(proposal.what_changes, empty="- None stated"),
        "",
        "## Out of scope",
        "",
        *_bullets(proposal.out_of_scope, empty="- None stated"),
        "",
        "## Affected repositories",
        "",
        *(f"- `{item.repository}` ({item.role})" for item in proposal.affected_repositories),
        "",
        "## Acceptance criteria",
        "",
        *(f"- [ ] {_line(item)}" for item in proposal.acceptance_criteria),
        "",
        "## Risks",
        "",
        *_bullets(proposal.risks, empty="- None identified"),
        "",
        f"Proposed risk {proposal.proposed_risk}: {_line(proposal.risk_rationale)}",
        "",
        "## Rollout",
        "",
        *_bullets(proposal.rollout, empty="- None stated"),
        "",
        "## Kill switch",
        "",
        *_bullets(proposal.kill_switch, empty="- None stated"),
        "",
        "## Open questions",
        "",
        *_bullets(proposal.open_questions, empty="- None"),
        "",
        "## Independent review",
        "",
        f"- Reviewer: {review.provider}",
        f"- Decision: {review.decision} after {review.rounds} round(s)",
        f"- Summary: {_line(review.summary)}",
    ]
    if review.unresolved_findings:
        lines.append("- Unresolved findings:")
        lines.extend(
            f"  - [{item.category}] {_line(item.detail)}"
            + (f" ({_line(item.reference)})" if item.reference.strip() else "")
            for item in review.unresolved_findings
        )
    else:
        lines.append("- Unresolved findings: none")
    lines.extend(
        [
            "",
            "## Operator description",
            "",
            *(f"> {_line(line)}".rstrip() for line in description_redacted.strip().splitlines()),
            "",
            "Drafted by Hermes from the operator description above and published only after "
            "a typed operator approval bound to this exact content.",
        ]
    )
    body_core = "\n".join(lines) + "\n"
    body_sha256 = hashlib.sha256(body_core.encode("utf-8")).hexdigest()
    binding = approval_binding(
        change_request_id=change_request_id,
        revision=revision,
        fingerprint=fingerprint,
        target_repository=target_repository,
        title=title,
        body_sha256=body_sha256,
    )
    marker = change_request_marker(
        change_request_id=change_request_id,
        revision=revision,
        fingerprint=fingerprint,
        binding=binding,
    )
    return RenderedChangeRequest(
        title=title,
        body=f"{body_core}\n{marker}\n",
        body_sha256=body_sha256,
        fingerprint=fingerprint,
        approval_binding=binding,
        marker=marker,
    )


def change_proposal_schema() -> dict[str, Any]:
    """The drafter's structured-output contract (mirrors ChangeProposal)."""
    entries = {
        "type": "array",
        "maxItems": 15,
        "items": {"type": "string", "minLength": 1, "maxLength": 600},
    }
    required_entries = {**entries, "minItems": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "kind",
            "why",
            "what_changes",
            "out_of_scope",
            "affected_repositories",
            "acceptance_criteria",
            "risks",
            "rollout",
            "kill_switch",
            "open_questions",
            "proposed_risk",
            "risk_rationale",
        ],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 100},
            "kind": {"type": "string", "enum": list(CHANGE_REQUEST_KINDS)},
            "why": {"type": "string", "minLength": 1, "maxLength": 3000},
            "what_changes": required_entries,
            "out_of_scope": entries,
            "affected_repositories": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["repository", "role"],
                    "properties": {
                        "repository": {"type": "string", "pattern": _REPOSITORY_PATTERN},
                        "role": {"type": "string", "enum": ["source", "deployment"]},
                    },
                },
            },
            "acceptance_criteria": required_entries,
            "risks": entries,
            "rollout": entries,
            "kill_switch": entries,
            "open_questions": entries,
            "proposed_risk": {"type": "string", "enum": ["R0", "R1", "R2", "R3"]},
            "risk_rationale": {"type": "string", "minLength": 1, "maxLength": 1500},
        },
    }


def change_review_schema() -> dict[str, Any]:
    """The independent reviewer's structured-output contract (mirrors ChangeReview)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "summary", "findings"],
        "properties": {
            "decision": {"type": "string", "enum": ["APPROVE", "REVISE"]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "findings": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["category", "detail", "reference"],
                    "properties": {
                        "category": {"type": "string", "enum": list(REVIEW_CATEGORIES)},
                        "detail": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "reference": {"type": "string", "maxLength": 300},
                    },
                },
            },
        },
    }


_MARKDOWN_ESCAPES = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "`": "&#96;",
        "\\": "\\\\",
        "[": "\\[",
        "]": "\\]",
    }
)
# A leading character that could start Markdown block structure once a line is unprefixed.
_BLOCK_START = re.compile(r"^(?:[#+*=|~>_-]|\d+[.)])")


def _line(value: str) -> str:
    """One inert rendered line of untrusted text (LLM output or operator description).

    The text never spans lines, so it cannot start a kind/Risk line, heading, checkbox or
    marker; HTML (comments, tags), code spans, links and images are escaped, so it cannot
    hide or imitate the control-plane sections either.
    """
    flattened = (
        " ".join(value.split())
        .translate(_MARKDOWN_ESCAPES)
        # Keep redaction placeholders literal so re-redaction stays a no-op.
        .replace("\\[REDACTED\\]", "[REDACTED]")
    )
    return f"\\{flattened}" if _BLOCK_START.match(flattened) else flattened


def _bullets(values: tuple[str, ...], *, empty: str) -> list[str]:
    return [f"- {_line(item)}" for item in values] or [empty]
