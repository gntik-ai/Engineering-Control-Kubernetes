"""NEW-128: correction state, whole-word risk keywords and amend retry before a ChangeSet."""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_controlled_operations import CAMPAIGN_ID
from test_issue_delivery_approval import _child, _dispose
from test_issue_operation_approval import _approved_direct_request

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.config import Settings
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    OperationRequest,
    Run,
    Step,
)
from engineering_control.engineering import backlog
from engineering_control.engineering.backlog import (
    BacklogIssueType,
    GitHubIssueSnapshot,
    calculated_issue_risk,
    classify_issue,
)
from engineering_control.operations.issue_amend import retry_workflow_id
from engineering_control.operations.issue_start import (
    issue_request_from_operation,
    issue_workflow_id,
)
from engineering_control.operations.models import SolveIssueCommand

NOW = datetime(2026, 10, 4, tzinfo=UTC)


def _scan(tmp_path: Path) -> ScanActivityHandler:
    return ScanActivityHandler(
        Settings(
            _env_file=None,
            worktree_root=tmp_path / "scan-worktrees",
            evidence_root=tmp_path / "scan-evidence",
        )
    )


def _validated(tmp_path: Path, loaded: dict[str, Any], *, checker: str, maker_tests: Any = None):
    """The real scan-worker verdict for one attempt (it has no database access)."""
    payload: dict[str, Any] = {
        **loaded,
        "agent_result": {"structured_result": {"decision": checker}},
    }
    if maker_tests is not None:
        payload["source_maker_result"] = {"structured_result": {"tests": maker_tests}}
    return _scan(tmp_path)._validate_changeset(payload)


def _planned(handler: Any, change_set_id: str) -> None:
    # delivery.create_openspec moves every attempt's ChangeSet to PLANNED.
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        change_set.state = "PLANNED"


def _state(handler: Any, change_set_id: str) -> tuple[str, int, dict[str, Any]]:
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        return change_set.state, change_set.version, dict(change_set.recovery_json or {})


def _next_child(engineering: Any, handler: Any, payload: dict[str, Any], child: dict, name: str):
    registered = engineering._register_backlog_child_run({**payload, "child_workflow_id": name})
    return handler._load_confirmed_finding(
        {
            **child,
            "issue_approval_id": registered["issue_approval_id"],
            "orchestration": {"workflow_id": name},
        }
    )


@pytest.mark.asyncio
async def test_checker_request_changes_persists_the_state_and_the_correction_binds(
    tmp_path: Path,
) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    _, version, _ = _state(handler, change_set_id)

    validated = _validated(tmp_path, first, checker="REQUEST_CHANGES")
    assert (validated["status"], validated["blocker"]) == (
        "REQUEST_CHANGES",
        "INDEPENDENT_CHECKER_DID_NOT_PASS",
    )
    assert handler._release_delivery_leases(validated)["status"] == "LEASES_RELEASED"
    state, after, recovery = _state(handler, change_set_id)
    assert (state, after) == ("REQUEST_CHANGES", version + 1)
    assert recovery["correction_requested"]["run_id"] == first["run_id"]
    assert recovery["correction_requested"]["blocker"] == "INDEPENDENT_CHECKER_DID_NOT_PASS"
    # An activity retry of the release changes nothing (exactly once per attempt).
    handler._release_delivery_leases(validated)
    assert _state(handler, change_set_id)[:2] == ("REQUEST_CHANGES", version + 1)

    second = _next_child(engineering, handler, payload, child, "backlog-fixture-issue-1-attempt-2")
    assert second["status"] == "CONFIRMED_LOADED"
    assert second["change_set_id"] == change_set_id
    _dispose(values)


@pytest.mark.asyncio
async def test_maker_test_failure_then_an_operator_retry_binds(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    # The checker PASSed but the maker reported no test record: MAKER_TEST_FAILURE.
    validated = _validated(tmp_path, first, checker="PASS", maker_tests="not-a-list")
    assert (validated["status"], validated["blocker"]) == ("BLOCKED", "MAKER_TEST_FAILURE")
    handler._release_delivery_leases(validated)
    assert _state(handler, first["change_set_id"])[0] == "REQUEST_CHANGES"
    retried = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    assert retried["status"] == "CONFIRMED_LOADED"
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "context"),
    [
        ("SOURCE_PR", {"status": "REQUEST_CHANGES", "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS"}),
        ("SOURCE_MERGED", {"status": "BLOCKED", "blocker": "MAKER_TEST_FAILURE"}),
        (
            "RUNTIME_VERIFIED",
            {"status": "REQUEST_CHANGES", "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS"},
        ),
        ("CONFIRMED", {"status": "REQUEST_CHANGES", "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS"}),
        ("PLANNED", {"status": "BLOCKED", "blocker": "SOURCE_MAKER_DID_NOT_COMPLETE"}),
        ("PLANNED", {"status": "CANCELLED"}),
    ],
)
async def test_published_or_non_correction_stops_never_move_the_change_set(
    tmp_path: Path, state: str, context: dict[str, Any]
) -> None:
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = state
        if state != "CONFIRMED" and state != "PLANNED":
            change_set.source_pr_url = "https://github.com/gntik-ai/falcone/pull/7"
    before = _state(handler, first["change_set_id"])
    handler._release_delivery_leases({**first, **context})
    assert _state(handler, first["change_set_id"]) == before
    _dispose(values)


@pytest.mark.asyncio
async def test_another_change_set_in_the_context_is_never_moved(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    handler._release_delivery_leases(
        {
            **first,
            "change_set_id": "00000000-0000-4000-8000-000000000128",
            "status": "REQUEST_CHANGES",
            "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS",
        }
    )
    assert _state(handler, first["change_set_id"])[0] == "PLANNED"
    _dispose(values)


@pytest.mark.asyncio
async def test_the_bounded_lineage_still_caps_corrections(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    limit = int(payload["correction_limit"])
    loaded = handler._load_confirmed_finding(child)
    for attempt in range(2, limit + 3):
        _planned(handler, loaded["change_set_id"])
        handler._release_delivery_leases(_validated(tmp_path, loaded, checker="REQUEST_CHANGES"))
        name = f"backlog-fixture-issue-1-attempt-{attempt}"
        if attempt <= limit + 1:
            loaded = _next_child(engineering, handler, payload, child, name)
            assert loaded["status"] == "CONFIRMED_LOADED"
        else:
            with pytest.raises(ValueError, match="exceeds the approved correction limit"):
                _next_child(engineering, handler, payload, child, name)
    _dispose(values)


def _issue(title: str, body: str, labels: tuple[str, ...] = ("bug",)) -> GitHubIssueSnapshot:
    import hashlib

    return GitHubIssueSnapshot(
        repository="gntik-ai/falcone",
        number=128,
        url="https://github.com/gntik-ai/falcone/issues/128",
        title=title,
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=labels,
        assignees=(),
        comments=(),
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    "body",
    [
        "A source-reproduction of the failure is attached.",
        "The bug is reproducible with the minimal reproduction below.",
        "Restores the metadata migration order.",
    ],
)
def test_risk_keywords_do_not_match_inside_other_words(body: str) -> None:
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", body) == "R1"


@pytest.mark.parametrize(
    "body",
    [
        "This breaks production traffic.",
        "Requires a data migration of the tenant table.",
        "Two data\nmigrations are pending.",
        "A Destructive cleanup removes volumes.",
        "Grants a new ClusterRoleBinding to the operator.",
        "Staging and non-production differ.",
    ],
)
def test_risk_keywords_still_match_whole_words_and_phrases(body: str) -> None:
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", body) == "R3"


def test_severity_and_label_keywords_match_whole_words() -> None:
    def classify(body: str, labels: tuple[str, ...] = ("bug",)) -> Any:
        return classify_issue(
            _issue("API returns 500", body, labels),
            (),
            project="falcone",
            import_filter="all",
            evaluated_at=NOW,
        )

    assert classify("Metadata loss is not involved.").severity == "unspecified"
    assert classify("Silent data loss on restart.").severity == "critical"
    # "metadata" is not the "meta" label, "decision" is not "ci".
    assert classify("x", ("metadata",)).issue_type is BacklogIssueType.PRODUCT_BUG
    assert classify("x", ("area/ci",)).issue_type is BacklogIssueType.META_TOOLING
    assert classify("x", ("type:security",)).issue_type is BacklogIssueType.SECURITY_DEFECT


@pytest.mark.asyncio
async def test_amend_retry_resolves_the_paused_loop_before_any_change_set(
    tmp_path: Path,
) -> None:
    operations, metadata, handler, request = await _approved_direct_request(tmp_path)
    loop_id = issue_workflow_id(request)
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        assert session.scalar(select(ChangeSet)) is None
        # Without an active loop Run nothing is resolved (BACKLOG_LOOP_NOT_FOUND).
        assert retry_workflow_id(session, None, issue) is None
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=loop_id,
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # The issue operation's loop paused at CURRENT_REVALIDATION_FLAKY, before a child.
        assert retry_workflow_id(session, None, issue) == loop_id
        assert retry_workflow_id(session, None) is None
    with operations.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == loop_id))
        run.status, run.finished_at = "STOPPED", NOW
        # NEW-128 review 2: campaign membership alone no longer proves ownership.
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=f"backlog-loop-falcone-{CAMPAIGN_ID}-campaign",
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert retry_workflow_id(session, None, issue) is None
    with operations.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=f"backlog-loop-falcone-{CAMPAIGN_ID}-other",
                run_type="existing-backlog-loop",
                status="RUNNING",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # Campaign loops without a registered child or issue-start binding prove nothing.
        assert retry_workflow_id(session, None, issue) is None
    for item in (operations, metadata, handler):
        item.engine.dispose()


@pytest.mark.asyncio
async def test_amend_retry_prefers_a_registered_child_approval_loop(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    operations = values[0]
    with operations.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id="backlog-fixture-parent",
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # A child was registered but never loaded, so no ChangeSet exists yet.
        assert session.scalar(select(ChangeSet)) is None
        assert retry_workflow_id(session, None, issue) == "backlog-fixture-parent"
    _dispose(values)


# --- Codex review of 4ff2a1a ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_delayed_duplicate_release_of_a_superseded_attempt_changes_nothing(
    tmp_path: Path,
) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    stopped = _validated(tmp_path, first, checker="REQUEST_CHANGES")
    handler._release_delivery_leases(stopped)
    assert _state(handler, change_set_id)[0] == "REQUEST_CHANGES"
    second = _next_child(engineering, handler, payload, child, "backlog-fixture-issue-1-attempt-2")
    _planned(handler, change_set_id)
    before = _state(handler, change_set_id)
    # Attempt 1's release is delivered again after attempt 2 moved the ChangeSet on.
    handler._release_delivery_leases(stopped)
    assert _state(handler, change_set_id) == before
    # Attempt 2's own stop is still recorded, once.
    handler._release_delivery_leases(_validated(tmp_path, second, checker="REQUEST_CHANGES"))
    state, version, recovery = _state(handler, change_set_id)
    assert (state, version) == ("REQUEST_CHANGES", before[1] + 1)
    assert recovery["correction_requested"]["run_ids"] == [first["run_id"], second["run_id"]]
    _dispose(values)


@pytest.mark.asyncio
async def test_a_first_release_from_a_superseded_attempt_never_writes(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    with handler.sessions.begin() as session:
        session.get(ChangeSet, UUID(change_set_id)).state = "REQUEST_CHANGES"
    _next_child(engineering, handler, payload, child, "backlog-fixture-issue-1-attempt-2")
    _planned(handler, change_set_id)
    before = _state(handler, change_set_id)
    handler._release_delivery_leases(_validated(tmp_path, first, checker="REQUEST_CHANGES"))
    assert _state(handler, change_set_id) == before
    _dispose(values)


def _keyword_terms() -> set[str]:
    """Every literal keyword the backlog classifier passes to its matcher."""
    tree = ast.parse(inspect.getsource(backlog))
    terms: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_mentions"
        ):
            for argument in node.args[1:]:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    terms.add(argument.value)
    return terms


def _regular_plural(term: str) -> str:
    if term.endswith(("s", "x", "z", "ch", "sh")):
        return term + "es"
    if term.endswith("y") and term[-2] not in "aeiou":
        return term[:-1] + "ies"
    return term + "s"


# The only substring matches dropped on purpose (other words that contain a keyword).
INTENDED_DROPS = {
    ("source-reproduction", "production"),
    ("minimal reproduction", "production"),
    ("reproducible", "producible"),
    ("metadata", "meta"),
}


def test_no_keyword_form_the_substring_matcher_caught_is_lost() -> None:
    terms = _keyword_terms()
    assert {"authorization bypass", "data loss"} <= terms
    lost = []
    for term in sorted(terms):
        plural = _regular_plural(term)
        forms = [term, plural, f"non-{term}", f"{term}-like", f"({plural})", f"the {term}."]
        if plural.endswith("ies"):
            forms = [form for form in forms if term not in form]  # never a substring match
            forms.append(plural)
        for form in forms:
            if term in form and not backlog._mentions(form, term):
                lost.append((form, term))
    assert lost == []
    for text, term in INTENDED_DROPS:
        assert term in text and not backlog._mentions(text, term)


def _classification(body: str, labels: tuple[str, ...]) -> tuple[Any, ...]:
    record = classify_issue(
        _issue("Report", body, labels), (), project="falcone", import_filter="all", evaluated_at=NOW
    )
    return (
        record.issue_type,
        record.severity,
        record.calculated_risk,
        record.reproduction_mode,
        record.planned_wave,
        record.documentation_levels,
    )


CORPUS = [
    ("Authorization bypasses in the gateway.", ("bug",)),
    ("Two data losses after restart.", ("bug",)),
    ("Productionise the gateway before the rollout.", ("bug",)),
    ("Productionized charts and productionizing jobs.", ("bug",)),
    ("Data migrations are pending.", ("bug",)),
    ("New ClusterRoles and ClusterRoleBindings.", ("bug",)),
    ("Credential rotations and secret lifecycles.", ("bug",)),
    ("Destructive cleanups.", ("bug",)),
    ("Tenant isolation breaks.", ("bug",)),
    ("Remote code execution in the parser.", ("bug",)),
    ("Logins fail; http 401s and http 404s on staging.", ("bug",)),
    ("Helm and ArgoCD gitops drift: degraded and outofsync.", ("bug",)),
    ("Build blockers and runtime blockers.", ("bug",)),
    ("Update the runbooks, the OpenAPI and the user guides for administrators.", ("bug",)),
    ("Proposal only; needs product decision.", ("bug",)),
    ("x", ("type:security",)),
    ("x", ("area/ci", "tooling")),
    ("x", ("docs",)),
    ("x", ("operations",)),
    ("x", ("duplicate",)),
    ("x", ("wontfix",)),
    ("x", ("questions",)),
]


def test_classification_matches_the_substring_classifier_except_false_positives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new = [_classification(body, labels) for body, labels in CORPUS]
    with monkeypatch.context() as patch:
        patch.setattr(
            backlog, "_mentions", lambda text, *terms: any(term in text for term in terms)
        )
        # The R3 gate's roots, matched as plain substrings like the old classifier.
        patch.setattr(
            backlog, "_R3_KEYWORDS", re.compile("|".join(backlog._R3_KEYWORD_ROOTS), re.IGNORECASE)
        )
        old = [_classification(body, labels) for body, labels in CORPUS]
        dropped = [
            _classification(body, labels)
            for body, labels in [
                ("A source-reproduction is attached.", ("bug",)),
                ("x", ("metadata",)),
            ]
        ]
    assert new == old
    assert new[0][0] is BacklogIssueType.SECURITY_DEFECT and new[0][2] == "R3"
    assert new[1][1] == "critical"
    assert new[2][2] == "R3"
    assert dropped[0][2] == "R3"  # the substring false positive
    assert _classification("A source-reproduction is attached.", ("bug",))[2] == "R1"
    assert dropped[1][0] is BacklogIssueType.META_TOOLING
    assert _classification("x", ("metadata",))[0] is BacklogIssueType.PRODUCT_BUG


@pytest.mark.parametrize(
    "body",
    [
        "Productionise the gateway before the rollout.",
        "Productionize the gateway.",
        "The productionised chart.",
        "Productionized images.",
        "While productionising the API.",
        "Productionizing the worker.",
        "It productionises the queue.",
    ],
)
def test_production_forms_stay_r3(body: str) -> None:
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", body) == "R3"


def _loop_run(workflow_id: str, created_at: datetime, status: str = "PAUSED") -> Run:
    return Run(
        campaign_id=None,
        change_set_id=None,
        workflow_id=workflow_id,
        run_type="existing-backlog-loop",
        status=status,
        project="falcone",
        started_at=created_at,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_amend_retry_prefers_the_loop_that_holds_the_issue_now(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    operations, _metadata, engineering, _handler, payload, _registered, _child_payload = values
    old_loop = f"backlog-loop-falcone-{CAMPAIGN_ID}-{uuid4()}"
    with operations.sessions.begin() as session:
        operation = session.scalar(select(OperationRequest))
        issue_loop = issue_workflow_id(issue_request_from_operation(operation))
        session.add(_loop_run(old_loop, NOW - timedelta(hours=2)))
        session.add(_loop_run(issue_loop, NOW))
    # The old campaign loop registered a child for this issue earlier.
    engineering._register_backlog_child_run(
        {
            **payload,
            "child_workflow_id": "backlog-old-issue-1-attempt-1",
            "orchestration": {"workflow_id": old_loop},
        }
    )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert retry_workflow_id(session, None, issue) == issue_loop
    with operations.sessions.begin() as session:
        loop = session.scalar(select(Run).where(Run.workflow_id == issue_loop))
        loop.status, loop.finished_at = "STOPPED", NOW
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert retry_workflow_id(session, None, issue) == old_loop
        # The old loop then registers a child for another issue: it moved on.
        holder = session.scalar(select(Run).where(Run.workflow_id == old_loop))
        holder_id = holder.id
    with operations.sessions.begin() as session:
        session.add(
            Step(
                run_id=holder_id,
                name="delivery.issue_approval",
                ordinal=0,
                status="COMPLETE",
                idempotency_key=f"new-128-{uuid4()}",
                attempt=1,
                result_json={
                    "parent_workflow_id": old_loop,
                    "issue_repository": "gntik-ai/falcone",
                    "issue_number": 43,
                },
                created_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert retry_workflow_id(session, None, issue) is None
    _dispose(values)


@pytest.mark.asyncio
async def test_amend_retry_never_picks_another_issues_start_loop(tmp_path: Path) -> None:
    operations, metadata, handler, _request = await _approved_direct_request(tmp_path)
    other = SolveIssueCommand(
        project="falcone",
        backlog_campaign_id=CAMPAIGN_ID,
        repository="gntik-ai/falcone",
        issue_number=43,
        delivery_mode="source-pr",
    )
    operation = OperationRequest(
        id=uuid4(),
        action="solve_issue",
        project="falcone",
        target_type="github_issue",
        target_id="gntik-ai/falcone#43",
        payload_json=other.model_dump(mode="json"),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key="new-128-other-issue",
        binding_hash=hashlib.sha256(b"new-128").hexdigest(),
        status="STARTED",
        created_at=NOW,
    )
    other_loop = issue_workflow_id(issue_request_from_operation(operation))
    with operations.sessions.begin() as session:
        session.add(operation)
        session.add(_loop_run(other_loop, NOW))
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue.issue_number == 42
        # The campaign's only active loop is #43's issue-start loop: not this issue's.
        assert retry_workflow_id(session, None, issue) is None
    for item in (operations, metadata, handler):
        item.engine.dispose()


@pytest.mark.parametrize(
    "text",
    [
        "Productionisation of the gateway",
        "productionization plan",
        "Productionise the gateway before the rollout",
        "a production outage",
        "purge destructively",
        "check the destructiveness of the cleanup",
        "two data migrations",
        "credential rotation job",
        "clusterrolebindings are too broad",
        "secret lifecycle handling",
        "Deploy to _production_.",
        "a _destructive_ cleanup",
        "**data migration** first",
        "__clusterrole__ grants",
        "the production-ready image",
    ],
)
def test_r3_gate_keeps_every_inflection_of_its_roots(text: str) -> None:
    # NEW-128 review 2: the R3 gate over-matches by design (word-start prefixes).
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", text) == "R3"


@pytest.mark.parametrize(
    "text",
    ["A source-reproduction is attached.", "The bug is reproducible.", "minimal reproduction"],
)
def test_r3_gate_ignores_roots_inside_other_words(text: str) -> None:
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", text) == "R1"


@pytest.mark.parametrize(
    ("labels", "body"),
    [
        (("bug", "security", "severity:high"), "Five roles are inert on every structural route."),
        (("type:security",), "Roles are ignored."),
        (("bug",), "A tenant isolation gap lets one tenant read another's workspace."),
        (("bug",), "This is an authorization bypass on the executor."),
    ],
)
def test_security_defects_need_operator_r3_approval(labels: tuple[str, ...], body: str) -> None:
    # NEW-179: falcone #1008 (labels bug/security/severity:high) imported as R2 and merged
    # without an operator decision; a security defect is now R3 at import.
    assert calculated_issue_risk("gntik-ai/falcone", labels, "bug: roles are inert", body) == "R3"


def test_high_severity_and_security_enhancements_keep_their_risk() -> None:
    # Only security defects are raised: severity alone stays R2, and a security-labelled
    # enhancement without defect evidence stays an enhancement (not R3).
    assert (
        calculated_issue_risk("gntik-ai/falcone", ("bug", "severity:high"), "bug: slow", "Slow.")
        == "R2"
    )
    assert (
        calculated_issue_risk(
            "gntik-ai/falcone",
            ("enhancement", "security"),
            "enhancement(worker-profile): isolated worker profile",
            "Adds a worker profile.",
        )
        == "R1"
    )
