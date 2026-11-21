"""Regression checks for prerequisite evidence, direction, project scope and rubric pinning."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from engineering_control.dashboard import review_evidence, review_graph, review_prerequisites
from engineering_control.db.models import BacklogItem, ReviewEvidenceJob, ReviewFlowRun
from engineering_control.models import decisions
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed


def test_only_named_directional_dependencies_are_extracted() -> None:
    body = """## Dependencies
- #123
- other/service#5
Depends on: #45 and https://github.com/OTHER/Repo/issues/36.
#45 supplies the identifier.
Blocks #17
Does not depend on #22
See #700
```
Depends on: #600
```
Blocked by: #39 but not #18
"""
    declarations = review_prerequisites.declarations({"repository": "Owner/Repo", "body": body})
    assert set(declarations) == {
        "owner/repo#123",
        "other/service#5",
        "owner/repo#45",
        "other/repo#36",
        "owner/repo#39",
    }
    assert "#45 supplies the identifier" in declarations["owner/repo#45"]


def test_preview_uses_current_review_and_carries_evidence_to_provider(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        run_id = uuid4()
        with repo.sessions.begin() as db:
            issue = db.scalar(
                select(BacklogItem).where(
                    BacklogItem.project_id == data["project"], BacklogItem.issue_number == 41
                )
            )
            issue.description = (
                "A durable audit entry for every invocation.\nDepends on: #45.\n"
                "#45 provides the import sequence."
            )
        base = review_evidence.preview(
            repo.sessions, "falcone", None, "", "", 1, "Append the invocation ledger"
        )
        target = next(p for p in base["items"] if p["story"]["title"] == "Audit executions")
        ik, sk = target["id"].split("::")
        with repo.sessions.begin() as db:
            db.add(
                ReviewFlowRun(
                    id=run_id,
                    project_id=data["project"],
                    flow_key="review-evidence",
                    flow_name="Evidence",
                    action="evidence",
                    version=0,
                    settings={},
                )
            )
            db.flush()
            db.add(
                ReviewEvidenceJob(
                    run_id=run_id,
                    project_id=data["project"],
                    provider="",
                    model="",
                    token_cap=0,
                    state="complete",
                    source={"records": {ik: target["issue"], sk: target["story"]}},
                    result={
                        "sample": {"issue": ik, "story": sk},
                        "advisory": {"scope": "full", "status": "advisory"},
                    },
                )
            )

        def chosen():
            view = review_evidence.preview(
                repo.sessions, "falcone", None, "", "", 1, "Unrelated work"
            )
            assert view["model_calls"] == 0
            return next(p for p in view["items"] if p["story"]["title"] == "Audit executions")

        upstream = chosen()
        assert upstream["reason"] == "documented prerequisite"
        (citation,) = upstream["prerequisites"]
        assert citation["revision"] == target["issue"]["revision"][:16]
        assert "provides the import sequence" in citation["passage"]
        with repo.sessions.begin() as db:
            other_run = uuid4()
            earlier = db.get_one(ReviewEvidenceJob, run_id)
            db.add(
                ReviewFlowRun(
                    id=other_run,
                    project_id=data["project"],
                    flow_key="review-evidence",
                    flow_name="Evidence",
                    action="evidence",
                    version=0,
                    settings={},
                )
            )
            db.flush()
            db.add(
                ReviewEvidenceJob(
                    run_id=other_run,
                    project_id=data["project"],
                    provider="",
                    model="",
                    token_cap=0,
                    state="complete",
                    source=earlier.source,
                    claimed_at=datetime.now(UTC),
                    result={
                        "sample": {"issue": ik, "story": sk},
                        "advisory": {"scope": "partial", "status": "advisory"},
                    },
                )
            )
        assert chosen()["prerequisites"] == [citation]
        with repo.sessions() as db:
            from engineering_control.dashboard.project_records import active_project

            src = review_graph.snapshot(db, active_project(db, "falcone"), False, "", "", "")
            src["reviewed_scopes"], _ = review_prerequisites.review_history(
                db, data["project"], src["records"]
            )
            pairs, _ = review_graph.candidates(src, 1, prerequisites=True)
            pair = next(p for p in pairs if p["issue"] + "::" + p["story"] == upstream["id"])
            job = db.get_one(ReviewEvidenceJob, run_id)
            assert json.loads(review_evidence.question(job, src, {}, pair, "decision").prompt)[
                "dependency_sources"
            ] == [citation]
            src["decisions"][pair["issue"]] = {sk: "dismissed"}
            assert not any(
                p["issue"] == pair["issue"] and p["story"] == sk
                for p in review_graph.candidates(src, 1, prerequisites=True)[0]
            )
        with repo.sessions.begin() as db:
            job = db.get_one(ReviewEvidenceJob, run_id)
            job.result = {
                **job.result,
                "human_review": {"scope": "unrelated", "at": datetime.now(UTC).isoformat()},
            }
        assert not chosen()["prerequisites"]
        with repo.sessions.begin() as db:
            job = db.get_one(ReviewEvidenceJob, run_id)
            job.result = {
                **job.result,
                "human_review": {"scope": "full", "at": datetime.now(UTC).isoformat()},
            }
            issue = db.scalar(
                select(BacklogItem).where(
                    BacklogItem.project_id == data["project"], BacklogItem.issue_number == 41
                )
            )
            issue.title += " (revised)"
        assert not chosen()["prerequisites"]
    finally:
        client.close()


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6-luna"), ("openrouter", "google/gemini-decision-1")]
)
def test_v2_separates_scope_from_contribution_and_v1_keeps_its_wire_format(
    provider: str, model: str
) -> None:
    def names(rubric: str) -> set[str]:
        qs = decisions.body(provider, model, "system", "source", True, {}, rubric)["questions"]
        return set(qs) if isinstance(qs, dict) else {q["name"] for q in qs}

    assert names("issue-story-v1") == {"link", "scope", "strength"}
    assert names(decisions.RUBRIC) == {"link", "scope", "strength", "contribution"}
    answers = {
        "link": {"type": "noul", "noul": 0.99},
        "scope": {"type": "choice", "choice": "partial"},
        "contribution": {"type": "choice", "choice": "prerequisite"},
    }

    def read():
        wire = (
            answers
            if provider == "openrouter"
            else [
                {"name": k, **({"type": "predicate", "probability": 0.99} if k == "link" else v)}
                for k, v in answers.items()
            ]
        )
        return decisions.judgment(provider, wire, False, expanded=True)

    assert decisions.consistent(read())
    answers["scope"]["choice"] = "full"
    assert not decisions.consistent(read()) and decisions.needs_second_opinion(read())
    del answers["contribution"]
    assert not decisions.consistent(read())


def test_disagreement_with_an_existing_human_judgment_gets_an_independent_review() -> None:
    positive = {"scope": "partial", "contribution": "prerequisite", "link_probability": 0.9}
    negative = {"scope": "unrelated", "contribution": "none", "link_probability": 0.1}
    assert decisions.needs_second_opinion(positive, "unrelated")
    assert not decisions.needs_second_opinion(positive, "partial")
    for first, second in ((positive, positive), (positive, negative), (negative, positive)):
        result = decisions.advisory(first, second, "unrelated")
        assert result["status"] == "needs_review" and result["human_scope"] == "unrelated"
    assert decisions.advisory(negative, negative, "unrelated")["scope"] == "unrelated"
