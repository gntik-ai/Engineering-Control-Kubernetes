"""Bringing a project's records over from the original system, and cutting it over."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.bring_over import FORMAT, BringOver, Plan, export_script
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectRecords,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.base import Base
from engineering_control.db.models import (
    Approval,
    BacklogItem,
    Campaign,
    ChangeSet,
    CoverageCell,
    Enhancement,
    Finding,
    FindingVerification,
    ProjectRecord,
)
from engineering_control.projects import PROJECTS

REPOSITORY = "gntik-ai/falcone-workflow"
NOW = "2026-11-01T10:00:00+00:00"


def _stamps() -> dict[str, str]:
    return {"created_at": NOW, "updated_at": NOW}


def _finding(
    campaign: str, cell: str, disposition: str, actual: str, **more: Any
) -> dict[str, Any]:
    ident = str(uuid4())
    return {
        "id": ident,
        "campaign_id": campaign,
        "coverage_cell_id": cell,
        "fingerprint": f"sha256:{ident}",
        "project": "falcone-workflow",
        "domain": "api",
        "persona": "operator",
        "severity": "high",
        "confidence": 0.8,
        "risk_class": "R2",
        "disposition": disposition,
        "expected_behavior": "An expired session is refused.",
        "actual_behavior": actual,
        "evidence_json": [],
        "minimal_reproduction": ["Create a session.", "Wait.", "Call /me."],
        "affected_source_sha": "a" * 40,
        "affected_deployment_sha": None,
        "affected_image_digest": None,
        "affected_component": "session",
        "impact_json": {"security_impact": "The session keeps working.", "data_impact": None},
        "recommended_action": "Compare with the server's clock.",
        **_stamps(),
        **more,
    }


def _document() -> dict[str, Any]:
    campaign, cell = str(uuid4()), str(uuid4())
    open_finding = _finding(campaign, cell, "CONFIRMED", "Expired sessions are accepted.\nMore.")
    published = _finding(campaign, cell, "CONFIRMED", "Tokens are logged.")
    delivered = _finding(campaign, cell, "CONFIRMED", "The cookie lacks Secure.")
    rejected = _finding(campaign, cell, "REJECTED", "Nothing really.")
    issue_id, delivered_set, open_set = str(uuid4()), str(uuid4()), str(uuid4())
    backlog_campaign = str(uuid4())

    def change_set(
        ident: str, finding: dict[str, Any], state: str, pull: str | None
    ) -> dict[str, Any]:
        return {
            "id": ident,
            "finding_id": finding["id"],
            "issue_id": None,
            "project": "falcone-workflow",
            "risk_class": "R2",
            "state": state,
            "version": 1,
            "source_repository": REPOSITORY,
            "source_branch": "fix/x",
            "source_base_sha": "a" * 40,
            "source_head_sha": "b" * 40,
            "source_pr_url": pull,
            "deployment_repository": REPOSITORY,
            "deployment_branch": None,
            "deployment_base_sha": "a" * 40,
            "deployment_head_sha": None,
            "deployment_pr_url": None,
            "image_digests": {},
            "chart_version": None,
            "recovery_json": {},
            **_stamps(),
        }

    def enhancement(key: str, status: str, outcome: str, **payload: Any) -> dict[str, Any]:
        return {
            "id": str(uuid4()),
            "enhancement_key": key,
            "project": "falcone-workflow",
            "campaign_id": "campaign-1",
            "persona": "operator",
            "affected_repository": REPOSITORY,
            "risk_class": "R1",
            "status": status,
            "fingerprint": f"sha256:{key}",
            "approval_binding_sha256": "c" * 64,
            "payload_json": {
                "persona": "operator",
                "observed_problem": "Reports stay on the page.",
                "current_behavior": "No export.",
                "proposed_outcome": outcome,
                "user_value": "Reports can be shared.",
                "evidence_handles": ["artifact:1:abc"],
                "success_metric": "Exports per week.",
                "dependencies": [],
                "security_data_implications": "None.",
                "risk": "R1",
                "acceptance_criteria": ["A CSV downloads."],
                "rollout_strategy": "Behind a setting.",
                "kill_switch": None,
                **payload,
            },
            **_stamps(),
        }

    return {
        "format": FORMAT,
        "project": "falcone-workflow",
        "exported_at": "2026-11-13T09:00:00+00:00",
        "schema_revision": "0024",
        "tables": {
            "campaigns": [
                {
                    "id": campaign,
                    "project": "falcone-workflow",
                    "name": "Security audit",
                    "status": "COMPLETED",
                    "source_sha": "a" * 40,
                    "deployment_sha": None,
                    "required_cells": 1,
                    "metadata_json": {},
                    # A column this platform's table does not have is left behind.
                    "added_later": True,
                    **_stamps(),
                }
            ],
            "coverage_cells": [
                {
                    "id": cell,
                    "campaign_id": campaign,
                    "repository_surface": "source",
                    "domain": "api",
                    "persona": "operator",
                    "flow": "repository-static",
                    "lens": "security",
                    "environment": "source",
                    "status": "CONFIRMED_FINDING",
                    "blocker": None,
                    "exclusion_approval_id": None,
                    "evidence_json": [],
                    "result_json": {},
                    **_stamps(),
                }
            ],
            "findings": [open_finding, published, delivered, rejected],
            "finding_verifications": [
                {
                    "id": str(uuid4()),
                    "finding_id": open_finding["id"],
                    # The run it points to is not brought over: the pointer is dropped.
                    "run_id": str(uuid4()),
                    "verifier_provider": "claude",
                    "disposition": "CONFIRMED",
                    "reproduction_count": 2,
                    "positive_control": True,
                    "negative_control": True,
                    "duplicate_issue_url": None,
                    "summary": "Reproduced twice with both controls.",
                    **_stamps(),
                }
            ],
            "issues": [
                {
                    "id": issue_id,
                    "finding_id": published["id"],
                    "repository": REPOSITORY,
                    "number": 11,
                    "url": f"https://github.com/{REPOSITORY}/issues/11",
                    "state": "OPEN",
                    "openspec_change": None,
                    **_stamps(),
                }
            ],
            "change_sets": [
                change_set(
                    delivered_set, delivered, "SOURCE_MERGED", "https://github.com/x/pull/1"
                ),
                change_set(
                    open_set, published, "SOURCE_PR", f"https://github.com/{REPOSITORY}/pull/90"
                ),
                # It points to a finding that is not in the file: it is left out.
                change_set(str(uuid4()), {"id": str(uuid4())}, "PLANNED", None),
            ],
            "approvals": [
                {
                    "id": str(uuid4()),
                    "change_set_id": delivered_set,
                    "approval_type": "staging",
                    "decision": "APPROVE",
                    "artifact_binding_sha256": "d" * 64,
                    "approver_subject": "administrator",
                    "approved_at": NOW,
                    "invalidated_at": None,
                    "metadata_json": {},
                    **_stamps(),
                }
            ],
            "enhancements": [
                enhancement(
                    "enh-" + "1" * 32,
                    "CANDIDATE_ENHANCEMENT",
                    "Export the report as CSV.\nDetails.",
                ),
                enhancement("enh-" + "2" * 32, "APPROVED", "A dark mode."),
                enhancement("enh-" + "3" * 32, "REJECTED", "Sounds."),
                enhancement(
                    "enh-" + "4" * 32,
                    "PUBLISHED",
                    "Search while typing.",
                    issue_url=f"https://github.com/{REPOSITORY}/issues/12",
                ),
            ],
            "github_backlog_campaigns": [],
            "backlog_issue_records": [
                {
                    "id": str(uuid4()),
                    "backlog_campaign_id": backlog_campaign,
                    "repository": REPOSITORY,
                    "issue_number": 11,
                    "delivery_status": "DELIVERY_ACTIVE",
                    "final_closure_status": "NOT_STARTED",
                    "eligibility_status": "ELIGIBLE",
                    "change_set_id": open_set,
                    **_stamps(),
                }
            ],
        },
    }


@pytest.fixture
def site(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'bring.db'}")
    Base.metadata.create_all(engine)
    sessions: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    ProjectRecords(sessions).create(
        name="falcone-workflow",
        display_name="Falcone workflow",
        repository=REPOSITORY,
        default_branch="main",
    )
    SectionRecords(sessions).sync_core()
    with sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        # As the registry's projects are: known to the original system.
        project.origin = "registry"
        for number, title in ((11, "Tokens are logged"), (12, "Search while typing")):
            session.add(
                BacklogItem(
                    project_id=project.id,
                    title=title,
                    source="github",
                    state="proposed",
                    repository=REPOSITORY,
                    issue_number=number,
                    issue_url=f"https://github.com/{REPOSITORY}/issues/{number}",
                    issue_labels=[],
                )
            )

    class Site:
        def __init__(self) -> None:
            self.sessions = sessions
            self.backlog = BacklogItems(sessions)
            self.bring = BringOver(sessions, self.backlog)

        def count(self, model: Any) -> int:
            with sessions() as session:
                return int(session.scalar(select(func.count()).select_from(model)) or 0)

        def items(self) -> list[dict[str, Any]]:
            return self.backlog.listing(project="falcone-workflow", page_size=100)["items"]

    yield Site()
    engine.dispose()


def test_a_preview_says_what_would_come_and_stores_nothing(site: Any) -> None:
    assert site.bring.status("falcone-workflow") == {
        "origin": "registry",
        "here": False,
        "cut_over_at": None,
        "brought_over": None,
        "paused": None,
    }
    shown = site.bring.preview("falcone-workflow", _document())
    assert shown["findings"] == {"CONFIRMED": 3, "REJECTED": 1}
    # One confirmed finding is an issue already and one was delivered: one is left.
    assert shown["findings_to_propose"] == 1
    assert (shown["enhancements_to_propose"], shown["enhancements_to_accept"]) == (1, 1)
    assert (shown["issues_known"], shown["issues_in_backlog"], shown["issues_under_way"]) == (
        2,
        2,
        1,
    )
    assert shown["records"]["findings"] == 4 and shown["records"]["approvals"] == 1
    assert site.count(Finding) == 0 and len(site.items()) == 2
    assert site.bring.status("falcone-workflow")["here"] is False


def test_a_file_that_is_not_this_projects_export_is_refused(site: Any) -> None:
    with pytest.raises(ProjectInvalid, match="not a file of records"):
        site.bring.preview("falcone-workflow", {"format": "something/1"})
    with pytest.raises(ProjectInvalid, match="records of musematic, not of falcone-workflow"):
        site.bring.preview("falcone-workflow", {**_document(), "project": "musematic"})
    with pytest.raises(ProjectInvalid, match="cannot be read"):
        site.bring.preview("falcone-workflow", {**_document(), "tables": {"findings": "many"}})


def test_records_are_kept_open_work_becomes_items_and_the_project_is_cut_over(site: Any) -> None:
    document = _document()
    done = site.bring.apply("falcone-workflow", document)
    assert (done["items_proposed"], done["items_accepted"], done["issues_marked"]) == (2, 1, 2)
    # History is kept as it was, row by row; what points nowhere is left out.
    assert done["records_kept"] == {
        "campaigns": 1,
        "coverage_cells": 1,
        "findings": 4,
        "finding_verifications": 1,
        "issues": 1,
        "change_sets": 2,
        "approvals": 1,
        "enhancements": 4,
        "github_backlog_campaigns": 0,
        "backlog_issue_records": 0,
    }
    assert site.count(Campaign) == 1 and site.count(CoverageCell) == 1
    assert site.count(ChangeSet) == 2 and site.count(Enhancement) == 4
    with site.sessions() as session:
        approval = session.scalars(select(Approval)).one()
        # An approval stays bound to what it was given for: nothing is recomputed.
        assert approval.artifact_binding_sha256 == "d" * 64
        assert approval.approved_at.isoformat().startswith("2026-11-01T10:00:00")
        assert session.scalars(select(FindingVerification)).one().run_id is None

    items = {item["title"]: item for item in site.items()}
    finding = site.backlog.get(UUID(items["Expired sessions are accepted."]["id"]))
    assert (finding["state"], finding["proposed_by"], finding["section"]["key"]) == (
        "proposed",
        "original",
        "code-audit",
    )
    assert finding["facts"]["kind"] == "finding" and finding["facts"]["lens"] == "security"
    assert (
        "**Severity:** high · **Lens:** security · **Component:** session" in finding["description"]
    )
    assert "## Minimal reproduction\n1. Create a session." in finding["description"]
    assert (
        "Confirmed by the original system: Reproduced twice with both controls."
        in (finding["description"])
    )
    waiting = site.backlog.get(UUID(items["Export the report as CSV."]["id"]))
    assert (waiting["state"], waiting["section"]["key"]) == ("proposed", "enhancement")
    assert "## Evidence\n- artifact:1:abc" in waiting["description"]
    approved = site.backlog.get(UUID(items["A dark mode."]["id"]))
    assert approved["state"] == "accepted", "what was approved there is not asked again"
    assert "Sounds." not in items and "The cookie lacks Secure." not in items

    # The issues the original knew say what it had done about them.
    known = site.backlog.get(UUID(items["Tokens are logged"]["id"]))
    assert known["facts"]["kind"] == "finding" and known["facts"]["severity"] == "high"
    assert known["facts"]["original"]["pull_request"] == f"https://github.com/{REPOSITORY}/pull/90"
    assert known["facts"]["original"]["delivery"] == "DELIVERY_ACTIVE"
    published = site.backlog.get(UUID(items["Search while typing"]["id"]))
    assert published["facts"]["kind"] == "enhancement"
    assert published["facts"]["original"]["enhancement"] == "enh-" + "4" * 32

    status = site.bring.status("falcone-workflow")
    assert status["here"] is True and status["cut_over_at"] == done["cut_over_at"]
    assert status["brought_over"]["items_proposed"] == 2

    # The same file again changes nothing.
    again = site.bring.apply("falcone-workflow", document)
    assert (again["items_proposed"], again["items_accepted"]) == (0, 0)
    assert set(again["records_kept"].values()) == {0}
    assert len(site.items()) == 5 and site.count(Finding) == 4
    after = site.bring.status("falcone-workflow")
    assert after["cut_over_at"] == done["cut_over_at"], "the day it moved stays the day it moved"
    assert after["brought_over"]["items_proposed"] == 2


def test_a_project_can_be_cut_over_with_nothing_to_bring(site: Any) -> None:
    moved = site.bring.cut_over("falcone-workflow")
    assert moved["here"] is True and moved["brought_over"] is None
    assert site.bring.cut_over("falcone-workflow")["cut_over_at"] == moved["cut_over_at"]
    # A project made in the console never was the original's.
    ProjectRecords(site.sessions).create(
        name="made-here",
        display_name="Made here",
        repository="gntik-ai/made-here",
        default_branch="main",
    )
    assert site.bring.status("made-here")["here"] is True


def test_what_is_open_is_decided_by_the_originals_own_states() -> None:
    plan = Plan(_document())
    assert [finding["title"] for finding in plan.findings] == ["Expired sessions are accepted."]
    assert [(e["title"], e["state"]) for e in plan.enhancements] == [
        ("Export the report as CSV.", "proposed"),
        ("A dark mode.", "accepted"),
    ]
    assert plan.under_way == 1
    assert (
        Plan({"format": FORMAT, "project": "x", "tables": {}}).summary()["findings_to_propose"] == 0
    )


def test_the_export_script_reads_only_and_writes_what_the_import_takes(
    site: Any, tmp_path: Path
) -> None:
    """The script against a database that holds the original's tables: what it writes
    for a project is what the import understands."""
    site.bring.apply("falcone-workflow", _document())
    database = site.sessions.kw["bind"].url
    output = tmp_path / "records.json"
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_original_records.py"
    ran = subprocess.run(  # noqa: S603 - the repository's own script
        [
            sys.executable,
            str(script),
            "--project",
            "falcone-workflow",
            "--output",
            str(output),
            "--database-url",
            str(database),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    assert "findings: 4" in ran.stdout and str(database) not in ran.stdout
    assert output.stat().st_mode & 0o777 == 0o600
    exported = json.loads(output.read_text())
    assert exported["format"] == FORMAT and exported["project"] == "falcone-workflow"
    assert len(exported["tables"]["findings"]) == 4
    assert exported["tables"]["enhancements"][0]["payload_json"]["persona"] == "operator"
    assert Plan(exported).summary()["findings_to_propose"] == 1
    # A project the database does not know gives an empty file and says so.
    ran = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(script),
            "--project",
            "nobody",
            "--output",
            str(output),
            "--database-url",
            str(database),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0 and "Nothing was found." in ran.stdout


def test_nothing_is_moved_in_the_middle_of_a_run_of_the_original_unless_told(site: Any) -> None:
    running = {
        **_document(),
        "in_flight": [
            {
                "workflow_id": "issue-delivery-falcone-workflow-71",
                "run_type": "IssueDeliveryWorkflow",
                "status": "RUNNING",
                "started_at": NOW,
            },
            "not a run",
        ],
    }
    shown = site.bring.preview("falcone-workflow", running)
    assert shown["in_flight_count"] == 1
    assert shown["in_flight"] == [
        {
            "run": "IssueDeliveryWorkflow",
            "workflow": "issue-delivery-falcone-workflow-71",
            "status": "RUNNING",
        }
    ]
    with pytest.raises(ProjectConflict, match="still had 1 run"):
        site.bring.apply("falcone-workflow", running)
    assert site.count(Finding) == 0 and site.bring.status("falcone-workflow")["here"] is False
    done = site.bring.apply("falcone-workflow", running, even_if_running=True)
    assert done["items_proposed"] == 2 and site.bring.status("falcone-workflow")["here"] is True


def test_the_export_says_what_the_original_still_has_under_way(site: Any, tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from engineering_control.db.models import Run

    now = datetime.now(UTC)
    with site.sessions.begin() as session:
        for workflow, started, finished in (
            ("under-way", now - timedelta(hours=2), None),
            ("ended", now - timedelta(hours=3), now - timedelta(hours=1)),
            # A run that never ended, from long ago, is not what holds a move up.
            ("forgotten", now - timedelta(days=60), None),
        ):
            session.add(
                Run(
                    workflow_id=workflow,
                    run_type="IssueDeliveryWorkflow",
                    status="RUNNING" if finished is None else "COMPLETED",
                    project="falcone-workflow",
                    started_at=started,
                    finished_at=finished,
                )
            )
    output = tmp_path / "records.json"
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_original_records.py"
    ran = subprocess.run(  # noqa: S603 - the repository's own script
        [
            sys.executable,
            str(script),
            "--project",
            "falcone-workflow",
            "--output",
            str(output),
            "--database-url",
            str(site.sessions.kw["bind"].url),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    assert "still has 1 run(s) under way" in ran.stdout and "under-way (RUNNING)" in ran.stdout
    exported = json.loads(output.read_text())
    assert [run["workflow_id"] for run in exported["in_flight"]] == ["under-way"]
    assert Plan(exported).summary()["in_flight_count"] == 1


def test_the_command_line_does_what_the_page_does(site: Any, tmp_path: Path) -> None:
    """For an administrator with the cluster at hand: the script, a preview and the move,
    from a terminal that reaches the platform's database."""
    database = str(site.sessions.kw["bind"].url)
    exported = tmp_path / "records.json"
    exported.write_text(json.dumps(_document()))

    def ctl(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the package's own command
            [sys.executable, "-c", "from engineering_control.cli import main; main()", *arguments],
            env={**os.environ, "ENGINEERING_DATABASE_URL": database},
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    script = ctl("records", "script")
    assert script.returncode == 0 and "SET TRANSACTION READ ONLY" in script.stdout
    assert script.stdout == export_script()
    shown = ctl("records", "preview", "falcone-workflow", "--file", str(exported))
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["findings_to_propose"] == 1
    assert site.bring.status("falcone-workflow")["here"] is False
    # The file can come on standard input, as through kubectl exec -i.
    done = ctl("records", "bring-over", "falcone-workflow", stdin=exported.read_text())
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["items_proposed"] == 2
    assert site.bring.status("falcone-workflow")["here"] is True
    wrong = ctl("records", "bring-over", "musematic", "--file", str(exported))
    assert wrong.returncode == 1 and "not of musematic" in wrong.stderr
    # Only the projects the original knows have records there.
    assert ctl("records", "preview", "made-here", "--file", str(exported)).returncode == 2
    # Which projects are here, for the administrator who moves the rest in one pass.
    status = json.loads(ctl("records", "status").stdout)
    assert status["falcone-workflow"]["here"] is True
    assert status["musematic"]["here"] is False and set(status) == set(PROJECTS)
    # A project with nothing worth keeping is moved without a file.
    ProjectRecords(site.sessions).sync_registry()
    moved = ctl("records", "move-without-records", "llmwiki")
    assert moved.returncode == 0, moved.stderr
    assert json.loads(ctl("records", "status").stdout)["llmwiki"]["here"] is True
    # Without the database's address the command says so in one line.
    without = {k: v for k, v in os.environ.items() if k != "ENGINEERING_DATABASE_URL"}
    lost = subprocess.run(  # noqa: S603 - the package's own command
        [
            sys.executable,
            "-c",
            "from engineering_control.cli import main; main()",
            "records",
            "status",
        ],
        env=without,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lost.returncode == 1
    assert lost.stderr.strip() == "engineering-ctl: ENGINEERING_DATABASE_URL is unavailable"


def test_the_console_previews_brings_over_and_shows_where_the_work_is(site: Any) -> None:
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(str(site.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    document = _document()
    with client:
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        detail = client.get("/api/dashboard/projects/falcone-workflow").json()
        assert detail["work"]["here"] is False
        shown = post("/projects/falcone-workflow/bring-over/preview", records=document)
        assert shown.status_code == 200, shown.text
        assert shown.json()["findings_to_propose"] == 1
        wrong = post("/projects/falcone-workflow/bring-over/preview", records={"format": "x"})
        assert wrong.status_code == 422 and "not a file of records" in wrong.json()["detail"]
        # Bringing over changes the backlog and the project: it needs the password.
        assert post("/projects/falcone-workflow/bring-over", records=document).status_code == 422
        refused = post(
            "/projects/falcone-workflow/bring-over",
            records=document,
            current_password="nope",  # noqa: S106
        )
        assert refused.status_code == 403
        done = post(
            "/projects/falcone-workflow/bring-over", records=document, current_password=password
        )
        assert done.status_code == 200, done.text
        assert done.json()["items_proposed"] == 2
        # The script is handed out to be run on the original's host.
        script = client.get("/api/dashboard/original-records-script")
        assert script.status_code == 200 and script.text == export_script()
        assert 'filename="export_original_records.py"' in script.headers["content-disposition"]
        # A file written while the original still ran something is held back unless told.
        busy = {
            **document,
            "in_flight": [{"workflow_id": "w", "run_type": "X", "status": "RUNNING"}],
        }
        held = post(
            "/projects/falcone-workflow/bring-over", records=busy, current_password=password
        )
        assert held.status_code == 409 and "still had 1 run" in held.json()["detail"]
        moved = post(
            "/projects/falcone-workflow/bring-over",
            records=busy,
            current_password=password,
            even_if_running=True,
        )
        assert moved.status_code == 200 and moved.json()["items_proposed"] == 0
        status = client.get("/api/dashboard/projects/falcone-workflow/bring-over").json()
        assert status["here"] is True and status["brought_over"]["issues_marked"] == 2
        assert client.get("/api/dashboard/projects/nobody/bring-over").status_code == 404
        assert "falcone-workflow" in client.get("/api/dashboard/activity/recent").text
