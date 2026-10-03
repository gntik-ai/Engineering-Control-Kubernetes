"""NEW-114 bundle: activity-side behaviour of the backlog-loop fixes (2026-10-03).

Fix 1 (nothing delivered state + notice, free-slot selection), fix 2 (NEW-117 delivery
from revalidated heads), fix 3 (transient model capacity), fix 4 (retry/skip validated
against the loop) and fix 5 (Hermes's own operational issues are not backlog work).
The workflow halves are covered in test_workflow_replay.py (NEW-114 section).
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import engineering_control.activities.engineering as engineering_module
from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.process import (
    PROVIDER_CAPACITY_ERROR,
    ProcessResult,
    classify_failure,
)
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogIssueRecord,
    Campaign,
    ChangeSet,
    CoverageCell,
    Finding,
    GitHubBacklogCampaign,
    NotificationOutbox,
    Run,
)
from engineering_control.engineering.backlog import GitHubIssueSnapshot, classify_issue
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
from engineering_control.operations.repository import backlog_issue_signal_blocker
from engineering_control.worktrees.manager import GitWorktreeManager, WorktreeAllocation

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 3, tzinfo=UTC)
CAMPAIGN = UUID("11411411-4114-4114-8114-114114114114")
IMPORT_SOURCE, IMPORT_DEPLOYMENT = "a" * 40, "b" * 40
CURRENT_SOURCE, CURRENT_DEPLOYMENT = "c" * 40, "d" * 40


# -- shared fixtures ---------------------------------------------------------------------


def _backlog_campaign(status: str = "RUNNING") -> GitHubBacklogCampaign:
    return GitHubBacklogCampaign(
        id=CAMPAIGN,
        project="falcone",
        source_repository="gntik-ai/falcone",
        deployment_repository="gntik-ai/falcone-charts",
        imported_at=NOW,
        issue_query="state:open",
        import_filter="all",
        source_default_branch="main",
        deployment_default_branch="main",
        source_head_at_import=IMPORT_SOURCE,
        deployment_head_at_import=IMPORT_DEPLOYMENT,
        issue_count=3,
        eligible_count=3,
        deferred_count=0,
        duplicate_count=0,
        stale_count=0,
        already_fixed_count=0,
        existing_pr_count=0,
        blocked_count=0,
        dependency_graph_hash="e" * 64,
        plan_hash="f" * 64,
        approval_binding="9" * 64,
        approval_json={"approval": {"approved_by": "dashboard:reauthenticated"}},
        maximum_risk="R2",
        maximum_items=1,
        maximum_concurrency=1,
        delivery_mode="source-pr",
        include_enhancements=False,
        status=status,
        workflow_id=f"backlog-import-falcone-{CAMPAIGN}",
    )


def _issue_record(repository: str, number: int, *, title: str | None = None) -> BacklogIssueRecord:
    return BacklogIssueRecord(
        backlog_campaign_id=CAMPAIGN,
        repository=repository,
        issue_number=number,
        issue_url_reference=f"https://github.com/{repository}/issues/{number}",
        title=title or f"Bounded issue {number}",
        state="OPEN",
        labels_json=["bug"],
        milestone=None,
        issue_type="PRODUCT_BUG",
        openspec_kind=None,
        severity="MEDIUM",
        calculated_risk="R1",
        component="general",
        source_or_deployment_owner="source",
        documentation_levels_json=[],
        dependencies_json=[],
        blocking_issues_json=[],
        issues_blocked_json=[],
        linked_prs_json=[],
        linked_branches_json=[],
        linked_commits_json=[],
        duplicate_candidates_json=[],
        current_source_reproduction="PENDING_CURRENT_STATE_REVALIDATION",
        current_runtime_reproduction="NOT_REQUIRED",
        current_runtime_required=False,
        reproduction_mode="SOURCE_ONLY",
        candidate_status="IMPORTED",
        eligibility_status="ELIGIBLE_FOR_REVALIDATION",
        eligibility_reason="FILTER_MATCH_PENDING_REVALIDATION",
        planned_wave="W0",
        planned_order=number,
        finding_id=None,
        change_set_id=None,
        delivery_status="NOT_STARTED",
        final_closure_status="OPEN",
        documentation_impact="DOCUMENTATION_REQUIRED",
        body_sha256=hashlib.sha256(str(number).encode()).hexdigest(),
        github_updated_at=NOW,
        github_snapshot_json={"body_excerpt": f"bounded body {number}"},
    )


def _engineering_handler(tmp_path: Path) -> EngineeringActivityHandler:
    state_root = tmp_path / "state"
    (state_root / "github-policy-requests").mkdir(parents=True, mode=0o700)
    handler = EngineeringActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'new-114.sqlite'}",
            state_root=state_root,
            project_config_root=ROOT / "config/projects",
            persona_config_root=ROOT / "config/personas",
            flow_config_root=ROOT / "config/flows",
            credential_root=tmp_path / "credentials",
        )
    )
    Base.metadata.create_all(handler.engine)
    return handler


# -- fix 1: nothing delivered / free slots -----------------------------------------------


def test_a_loop_completing_with_nothing_delivered_says_so_and_notifies_once(
    tmp_path: Path,
) -> None:
    handler = _engineering_handler(tmp_path)
    workflow_id = f"backlog-loop-falcone-{CAMPAIGN}-1"
    with handler.sessions.begin() as session:
        session.add(_backlog_campaign())
        session.add(
            Run(
                workflow_id=workflow_id,
                run_type="existing-backlog-loop",
                status="RUNNING",
                project="falcone",
            )
        )
    payload = {
        "project": "falcone",
        "backlog_campaign_id": str(CAMPAIGN),
        "loop_status": "COMPLETE",
        "workflow_id": workflow_id,
        "completion": "COMPLETE_NOTHING_DELIVERED",
        "outcomes": ["gntik-ai/falcone-charts#45:BLOCKED:CURRENT_REVALIDATION_REJECTED"],
    }

    handler._record_backlog_loop_state(payload)
    # An activity retry after the commit (once the policy bridge consumed its request).
    for request in (tmp_path / "state" / "github-policy-requests").iterdir():
        request.unlink()
    handler._record_backlog_loop_state(payload)

    with handler.sessions() as session:
        run = session.scalars(select(Run).where(Run.workflow_id == workflow_id)).one()
        notices = session.scalars(select(NotificationOutbox)).all()
        campaign_status = session.get(GitHubBacklogCampaign, CAMPAIGN).status  # type: ignore[union-attr]
    assert (run.status, run.error_code) == ("COMPLETE", "COMPLETE_NOTHING_DELIVERED")
    assert run.finished_at is not None
    assert "falcone-charts#45:BLOCKED:CURRENT_REVALIDATION_REJECTED" in str(run.summary)
    assert campaign_status == "COMPLETE"
    assert len(notices) == 1
    assert notices[0].topic == "telegram.engineering-backlog-nothing-delivered"
    assert "nothing delivered" in notices[0].payload_json["message"]
    assert notices[0].payload_json["workflow_id"] == workflow_id

    with pytest.raises(ValueError, match="completion"):
        handler._record_backlog_loop_state({**payload, "loop_status": "PAUSED"})


def _approve_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        engineering_module,
        "_current_backlog_approval",
        lambda *_args, **_kwargs: SimpleNamespace(issue_approval=None),
    )
    monkeypatch.setattr(engineering_module, "require_backlog_issue_approval", lambda *_: None)


def test_selection_skips_held_issues_and_hermes_operational_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve_everything(monkeypatch)
    handler = _engineering_handler(tmp_path)
    with handler.sessions.begin() as session:
        session.add(_backlog_campaign())
        session.add(_issue_record("gntik-ai/falcone", 1))
        session.add(
            _issue_record(
                "gntik-ai/falcone-charts",
                2,
                title="Hermes staging Argo CD sync failed for change 0000",
            )
        )
        session.add(_issue_record("gntik-ai/falcone", 3))
    payload = {
        "project": "falcone",
        "backlog_campaign_id": str(CAMPAIGN),
        "maximum_risk": "R2",
        "delivery_mode": "source-pr",
        "maximum_items": 1,
    }

    first = handler._load_backlog_execution(payload)
    refill = handler._load_backlog_execution(
        {**payload, "exclude_issue_refs": ["gntik-ai/falcone#1"]}
    )

    assert [item["issue_number"] for item in first["items"]] == [1]
    # #2 is Hermes's own escalation issue: never selected, never consumes a slot.
    assert [item["issue_number"] for item in refill["items"]] == [3]


# -- fix 5: classification ---------------------------------------------------------------


def _snapshot(title: str, body: str) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        repository="gntik-ai/falcone-charts",
        number=45,
        url="https://github.com/gntik-ai/falcone-charts/issues/45",
        title=title,
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=("bug",),
        assignees=(),
        comments=(),
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    ("title", "body"),
    [
        (
            "Hermes staging Argo CD sync failed for change 1234",
            "Argo CD could not sync.\n<!-- engineering-control-staging-gitops:argo-sync:1234:a -->",
        ),
        ("Staging broken", "diagnosis\n<!-- engineering-control-staging-gitops:environment:9 -->"),
        ("Hermes staging environment unhealthy for change 77", "pods crash looping"),
    ],
)
def test_hermes_operational_issues_are_never_eligible(title: str, body: str) -> None:
    record = classify_issue(
        _snapshot(title, body), (), project="falcone", import_filter="all", evaluated_at=NOW
    )
    assert record.eligibility_status == "DEFERRED"
    assert record.eligibility_reason == "HERMES_OPERATIONAL_ISSUE"


def test_an_ordinary_bug_about_argo_is_still_eligible() -> None:
    record = classify_issue(
        _snapshot("Chart values break Argo CD sync", "The chart fails to render on Argo CD."),
        (),
        project="falcone",
        import_filter="all",
        evaluated_at=NOW,
    )
    assert record.eligibility_status == "ELIGIBLE_FOR_REVALIDATION"


# -- fix 4: retry/skip validation ----------------------------------------------------------


def test_an_issue_bound_signal_must_name_an_issue_of_the_loops_campaign(tmp_path: Path) -> None:
    handler = _engineering_handler(tmp_path)
    with handler.sessions.begin() as session:
        session.add(_backlog_campaign())
        session.add(_issue_record("gntik-ai/falcone-charts", 45))
    workflow_id = f"backlog-loop-falcone-{CAMPAIGN}-{uuid4()}"
    with handler.sessions() as session:
        # Live 2026-10-03: `backlog-retry <wf> falcone 45` although #45 is falcone-charts.
        assert backlog_issue_signal_blocker(session, workflow_id, "falcone", 45) == (
            "BACKLOG_ISSUE_NOT_IN_LOOP"
        )
        assert backlog_issue_signal_blocker(session, workflow_id, "musematic", 45) == (
            "BACKLOG_ISSUE_NOT_IN_LOOP_PROJECT"
        )
        assert backlog_issue_signal_blocker(session, workflow_id, "falcone-charts", 45) is None
        # Other workflows keep their behaviour.
        assert backlog_issue_signal_blocker(session, "issue-loop-x", "falcone", 45) is None


class _Handle:
    def __init__(self, state: dict[str, Any] | Exception) -> None:
        self.state = state

    async def query(self, _name: str) -> dict[str, Any]:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


def _control(state: dict[str, Any] | Exception) -> TemporalControlService:
    service = TemporalControlService.__new__(TemporalControlService)
    service.client = SimpleNamespace(get_workflow_handle=lambda _id: _Handle(state))  # type: ignore[assignment]
    return service


@pytest.mark.asyncio
async def test_a_retry_for_an_issue_the_loop_is_not_waiting_on_is_refused() -> None:
    waiting = {"phase": "AWAITING_OPERATOR:gntik-ai/falcone-charts#45:DELIVERY_FAILED:X"}
    await _control(waiting).validate_backlog_issue_signal("wf", "gntik-ai/falcone-charts#45")
    with pytest.raises(ValueError, match="waiting on gntik-ai/falcone-charts#45, not"):
        await _control(waiting).validate_backlog_issue_signal("wf", "gntik-ai/falcone#45")
    with pytest.raises(ValueError, match="not waiting on an issue"):
        await _control({"phase": "BACKLOG_ISSUE_2"}).validate_backlog_issue_signal(
            "wf", "gntik-ai/falcone#45"
        )
    # A loop that cannot be queried leaves the decision to the durable preflight.
    await _control(RuntimeError("unavailable")).validate_backlog_issue_signal(
        "wf", "gntik-ai/falcone#45"
    )


# -- fix 3: transient model capacity -------------------------------------------------------


def test_model_at_capacity_is_classified_as_transient_capacity() -> None:
    event = {"type": "error", "message": "Selected model is at capacity. Please try again."}
    assert classify_failure(ProcessResult(1, (event,), "")) == ("FAILED", PROVIDER_CAPACITY_ERROR)
    assert classify_failure(ProcessResult(1, (), "selected model is at capacity")) == (
        "FAILED",
        PROVIDER_CAPACITY_ERROR,
    )
    # Agent output that merely mentions capacity is not a provider error.
    message = {"type": "item.completed", "item": {"text": "the model is at capacity"}}
    assert classify_failure(ProcessResult(1, (message,), "boom")) == (
        "FAILED",
        "PROVIDER_EXIT_NONZERO",
    )


class _CapacityAdapter:
    provider = Provider.CODEX

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.requests: list[AgentRequest] = []

    async def authenticated(self) -> bool:
        return True

    async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
        self.requests.append(request)
        failed = len(self.requests) <= self.failures
        return AgentResult(
            run_id=request.run_id,
            provider=request.provider,
            role=request.role,
            status=RunStatus.FAILED if failed else RunStatus.SUCCESS,
            summary="Selected model is at capacity" if failed else "ok",
            structured_result={} if failed else {"summary": "ok"},
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            error_code=PROVIDER_CAPACITY_ERROR if failed else None,
        )


class _InstructionPack:
    def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, Any]:
        return request, SimpleNamespace(
            sha256="a" * 64, revision=None, sources=(), allowed_tools=()
        )


def _provider(tmp_path: Path, adapter: _CapacityAdapter) -> tuple[ProviderActivityHandler, dict]:
    worktree = tmp_path / "worktrees" / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),
        "codex",
        adapter,  # type: ignore[arg-type]
        _InstructionPack(),
    )
    handler._capacity_backoff_seconds = (0.0, 0.0, 0.0)
    request = AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        model="gpt-6.1-sol",
        prompt="make one bounded change",
        working_directory=str(worktree),
        writable=True,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    )
    return handler, {"maker_request": request.model_dump(mode="json")}


@pytest.mark.asyncio
async def test_a_capacity_failure_retries_the_same_invocation_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    adapter = _CapacityAdapter(failures=2)
    handler, payload = _provider(tmp_path, adapter)

    result = await handler.handle("delivery.invoke_maker", payload)

    assert result["status"] == "SUCCESS"
    assert len(adapter.requests) == 3
    # Never a substitute: the same provider, model and request every time.
    assert {(r.provider, r.model, r.run_id) for r in adapter.requests} == {
        (Provider.CODEX, "gpt-6.1-sol", adapter.requests[0].run_id)
    }


@pytest.mark.asyncio
async def test_capacity_retries_are_bounded_to_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    adapter = _CapacityAdapter(failures=10)
    handler, payload = _provider(tmp_path, adapter)

    result = await handler.handle("delivery.invoke_maker", payload)

    assert len(adapter.requests) == 4
    assert result["status"] == "FAILED"
    assert result["agent_result"]["error_code"] == PROVIDER_CAPACITY_ERROR


def test_production_capacity_backoff_is_two_five_and_ten_minutes() -> None:
    assert ProviderActivityHandler._capacity_backoff_seconds == (120.0, 300.0, 600.0)


# -- fix 2 (NEW-117): delivery from the revalidated heads ----------------------------------


class _HeadsManager:
    """Worktree double: current heads moved ahead of the import heads (fast-forward)."""

    def __init__(self, root: Path, *, fast_forward: bool = True) -> None:
        self.root = root
        self.fast_forward = fast_forward
        self.snapshots: list[dict[str, Any]] = []
        self.allocations: list[str] = []

    def resolve_head(self, repository: str, *, base_ref: str) -> str:
        return CURRENT_SOURCE if repository == "falcone" else CURRENT_DEPLOYMENT

    def is_fast_forward(self, repository: str, ancestor: str, head: str) -> bool:
        return ancestor == head or self.fast_forward

    def allocate_audit_snapshot(self, **values: Any) -> WorktreeAllocation:
        self.snapshots.append(values)
        target = self.root / values["repository"] / str(values["campaign_id"])
        target.mkdir(parents=True, exist_ok=True)
        return WorktreeAllocation(
            repository=values["repository"],
            path=target,
            branch="",
            base_sha=values["expected_base_sha"],
            change_set_id=values["campaign_id"],
        )

    def allocate(self, **values: Any) -> WorktreeAllocation:
        self.allocations.append(values["expected_base_sha"])
        return WorktreeAllocation(
            repository=values["repository"],
            path=self.root / values["repository"],
            branch=f"agent/{values['repository']}",
            base_sha=values["expected_base_sha"],
            change_set_id=values["change_set_id"],
        )


def _candidate(flow: str = "current-state-revalidation") -> dict[str, Any]:
    return {
        "finding_id": str(uuid4()),
        "project": "falcone",
        "affected_source_sha": IMPORT_SOURCE,
        "affected_deployment_sha": IMPORT_DEPLOYMENT,
        "flow": flow,
    }


def test_a_backlog_revalidation_snapshots_the_current_fast_forward_heads(tmp_path: Path) -> None:
    manager = _HeadsManager(tmp_path)
    handler = ScanActivityHandler(Settings(), manager)  # type: ignore[arg-type]

    snapshot = handler._fresh_finding_snapshot(_candidate())

    assert (snapshot["source_sha"], snapshot["deployment_sha"]) == (
        CURRENT_SOURCE,
        CURRENT_DEPLOYMENT,
    )
    assert (snapshot["import_source_sha"], snapshot["import_deployment_sha"]) == (
        IMPORT_SOURCE,
        IMPORT_DEPLOYMENT,
    )
    assert [values["expected_base_sha"] for values in manager.snapshots] == [
        CURRENT_SOURCE,
        CURRENT_DEPLOYMENT,
    ]
    # A snapshot of other heads never reuses the import-head snapshot's directory.
    assert str(manager.snapshots[0]["campaign_id"]) != snapshot["finding_id"]

    audit = handler._fresh_finding_snapshot(_candidate(flow="repository-static"))
    assert (audit["source_sha"], "import_source_sha" in audit) == (IMPORT_SOURCE, False)


def test_a_revalidation_refuses_heads_that_do_not_descend_from_the_import(tmp_path: Path) -> None:
    handler = ScanActivityHandler(
        Settings(),
        _HeadsManager(tmp_path, fast_forward=False),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="does not descend from the approved import head"):
        handler._fresh_finding_snapshot(_candidate())


def _allocation_request(source_base: str, approved: str | None) -> dict[str, Any]:
    request = {
        "change_set_id": str(uuid4()),
        "project": "falcone",
        "issue": "45",
        "source_repository": "falcone",
        "source_base_sha": source_base,
        "deployment_repository": "falcone-charts",
        "deployment_base_sha": CURRENT_DEPLOYMENT,
        "maker_provider": "codex",
        "checker_provider": "claude",
    }
    if approved is not None:
        request["source_approved_base_sha"] = approved
        request["deployment_approved_base_sha"] = IMPORT_DEPLOYMENT
    return request


def test_allocation_refuses_a_base_that_does_not_descend_from_the_approved_head(
    tmp_path: Path,
) -> None:
    allowed = _HeadsManager(tmp_path)
    handler = ScanActivityHandler(Settings(), allowed)  # type: ignore[arg-type]
    handler._allocate({"allocation_request": _allocation_request(CURRENT_SOURCE, IMPORT_SOURCE)})
    assert allowed.allocations == [CURRENT_SOURCE, CURRENT_DEPLOYMENT]

    refused = ScanActivityHandler(
        Settings(),
        _HeadsManager(tmp_path, fast_forward=False),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="does not descend"):
        refused._allocate(
            {"allocation_request": _allocation_request(CURRENT_SOURCE, IMPORT_SOURCE)}
        )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_fast_forward_check_against_a_real_mirror(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(
        work,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "1",
    )
    first = _git(work, "rev-parse", "HEAD")
    _git(
        work,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "2",
    )
    second = _git(work, "rev-parse", "HEAD")
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(mirrors / "falcone.git"))
    manager = GitWorktreeManager(
        mirror_root=mirrors,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
        metadata_root=tmp_path / "metadata",
    )

    assert manager.is_fast_forward("falcone", first, second)
    assert manager.is_fast_forward("falcone", first, first)
    assert not manager.is_fast_forward("falcone", second, first)
    assert not manager.is_fast_forward("falcone", first, "not-a-sha")


def _metadata_handler(tmp_path: Path) -> MetadataActivityHandler:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'delivery.sqlite'}",
            worktree_root=tmp_path / "worktrees",
            state_root=tmp_path / "state",
            project_config_root=ROOT / "config/projects",
        )
    )
    Base.metadata.create_all(handler.engine)
    return handler


def _confirmed_finding(handler: MetadataActivityHandler, impact: dict[str, Any]) -> Finding:
    campaign = Campaign(
        id=uuid4(),
        project="falcone",
        name="existing backlog",
        status="BACKLOG_REVALIDATION",
        source_sha=IMPORT_SOURCE,
        deployment_sha=IMPORT_DEPLOYMENT,
        required_cells=1,
        metadata_json={},
    )
    coverage = CoverageCell(
        id=uuid4(),
        campaign_id=campaign.id,
        domain="gateway",
        persona="existing-github-issue",
        lens="correctness",
        environment="source",
        status="CONFIRMED_FINDING",
        blocker=None,
        exclusion_approval_id=None,
        evidence_json=[],
        result_json={"provider": "claude"},
    )
    finding = Finding(
        id=uuid4(),
        campaign_id=campaign.id,
        coverage_cell_id=coverage.id,
        fingerprint=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        project="falcone",
        domain="gateway",
        persona="existing-github-issue",
        severity="medium",
        confidence=0.5,
        risk_class="R2",
        disposition="CONFIRMED",
        expected_behavior="bounded contract",
        actual_behavior="bounded mismatch",
        evidence_json=[],
        minimal_reproduction=[],
        affected_source_sha=IMPORT_SOURCE,
        affected_deployment_sha=IMPORT_DEPLOYMENT,
        affected_image_digest=None,
        affected_component="gateway",
        impact_json={"flow": "current-state-revalidation", **impact},
        recommended_action="fix",
    )
    with handler.sessions.begin() as session:
        session.add_all([campaign, coverage, finding])
    return finding


def _load(handler: MetadataActivityHandler, finding: Finding) -> dict[str, Any]:
    workflow_id = f"delivery-{uuid4()}"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                workflow_id=workflow_id,
                run_type="issue-delivery",
                status="QUEUED",
                project="falcone",
            )
        )
    return handler._load_confirmed_finding(
        {
            "finding_id": str(finding.id),
            "risk_class": "R2",
            "dry_run": True,
            "orchestration": {"workflow_id": workflow_id},
        }
    )


_REVALIDATED = {
    "source_sha": CURRENT_SOURCE,
    "deployment_sha": CURRENT_DEPLOYMENT,
    "import_source_sha": IMPORT_SOURCE,
    "import_deployment_sha": IMPORT_DEPLOYMENT,
    "snapshot_evidence_sha256": "5" * 64,
    "workflow_id": "verify",
}


def test_a_revalidated_issue_is_delivered_from_its_revalidated_heads(tmp_path: Path) -> None:
    handler = _metadata_handler(tmp_path)
    finding = _confirmed_finding(handler, {"revalidated_heads": _REVALIDATED})

    loaded = _load(handler, finding)
    routed = handler._route_delivery(loaded)

    assert (loaded["source_base_sha"], loaded["deployment_base_sha"]) == (
        CURRENT_SOURCE,
        CURRENT_DEPLOYMENT,
    )
    assert loaded["base_heads"] == {
        "import_source_sha": IMPORT_SOURCE,
        "import_deployment_sha": IMPORT_DEPLOYMENT,
        "delivery_source_sha": CURRENT_SOURCE,
        "delivery_deployment_sha": CURRENT_DEPLOYMENT,
        "moved_from_import": True,
    }
    assert routed["status"] == "ROUTED"
    allocation = routed["allocation_request"]
    assert allocation["source_base_sha"] == CURRENT_SOURCE
    assert allocation["source_approved_base_sha"] == IMPORT_SOURCE
    assert allocation["deployment_approved_base_sha"] == IMPORT_DEPLOYMENT


def test_heads_of_another_import_are_ignored_and_worked_change_sets_keep_their_base(
    tmp_path: Path,
) -> None:
    handler = _metadata_handler(tmp_path)
    foreign = {**_REVALIDATED, "import_source_sha": "7" * 40}
    finding = _confirmed_finding(handler, {"revalidated_heads": foreign})
    assert _load(handler, finding)["source_base_sha"] == IMPORT_SOURCE

    # A ChangeSet refused at allocation (no worktree yet) moves to the revalidated heads;
    # one that already has a branch keeps its base (NEW-64 resume semantics).
    moved = _confirmed_finding(handler, {})
    assert _load(handler, moved)["source_base_sha"] == IMPORT_SOURCE
    with handler.sessions.begin() as session:
        row = session.get(Finding, moved.id)
        assert row is not None
        row.impact_json = {**row.impact_json, "revalidated_heads": _REVALIDATED}
    assert _load(handler, moved)["source_base_sha"] == CURRENT_SOURCE

    kept = _confirmed_finding(handler, {})
    first = _load(handler, kept)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        assert change_set is not None
        change_set.source_branch = "agent/falcone/45/x"
        row = session.get(Finding, kept.id)
        assert row is not None
        row.impact_json = {**row.impact_json, "revalidated_heads": _REVALIDATED}
    assert _load(handler, kept)["source_base_sha"] == IMPORT_SOURCE


def test_a_confirmed_revalidation_records_its_heads_on_the_finding() -> None:
    finding = Finding(
        id=uuid4(),
        affected_source_sha=IMPORT_SOURCE,
        affected_deployment_sha=IMPORT_DEPLOYMENT,
        impact_json={},
    )
    snapshot = {
        "finding_id": str(finding.id),
        "source_sha": CURRENT_SOURCE,
        "deployment_sha": CURRENT_DEPLOYMENT,
        "import_source_sha": IMPORT_SOURCE,
        "import_deployment_sha": IMPORT_DEPLOYMENT,
        "snapshot_evidence_sha256": "5" * 64,
    }
    heads = MetadataActivityHandler._revalidated_heads(finding, {"snapshot": snapshot}, "verify")
    assert heads == {**_REVALIDATED}
    # An audit snapshot (no import heads) or another finding's snapshot records nothing.
    plain = {key: value for key, value in snapshot.items() if not key.startswith("import_")}
    assert MetadataActivityHandler._revalidated_heads(finding, {"snapshot": plain}, "v") is None
    other = {**snapshot, "finding_id": str(uuid4())}
    assert MetadataActivityHandler._revalidated_heads(finding, {"snapshot": other}, "v") is None
