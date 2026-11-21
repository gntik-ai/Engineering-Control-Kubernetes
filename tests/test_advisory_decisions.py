"""Exercise the conditional Decisions task through the evidence worker and review inbox."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from engineering_control.dashboard import evidence_verdicts, review_evidence, review_flows
from engineering_control.db.models import (
    ProjectIssueStory,
    ProjectReviewBudget,
    ProviderAccount,
    ReviewEvidenceJob,
    WorkspaceModelUsage,
)
from engineering_control.engine import reviews
from engineering_control.engine.validate import errors
from engineering_control.models import decisions, openrouter
from engineering_control.models.service import Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed


def test_second_opinion_requires_a_primary_and_the_evidence_action() -> None:
    docs = reviews.defaults()
    doc = docs[reviews.ADVISORY_FLOW]
    assert not errors(doc)
    assert doc.nodes[1].params["limit"] == 16
    assert docs["review-native-decisions"].nodes[1].params == {}
    for index, task in ((3, reviews.SECOND_OPINION), (-1, "review.coverage")):
        nodes = list(doc.nodes)
        nodes[index] = replace(nodes[index], task=task)
        assert reviews.invalid(replace(doc, nodes=tuple(nodes)))


@pytest.mark.parametrize(
    ("primary", "link", "second", "second_link", "pending"),
    [
        ("full", 1.0, None, None, False),
        ("partial", 0.8, None, None, False),
        ("unrelated", 0.0, "unrelated", 0.1, False),
        ("unrelated", 0.0, "unrelated", 0.1, True),
        ("unrelated", 0.2, "partial", 0.9, True),
        ("unrelated", 0.2, "partial", 0.2, True),
        ("partial", None, "partial", 0.9, True),
        ("full", 0.0, "full", 1.0, True),
        ("unclear", None, "unrelated", 0.0, True),
    ],
)
def test_conditional_review_and_replay(
    tmp_path: Path,
    primary: str,
    link: float | None,
    second: str | None,
    second_link: float | None,
    pending: bool,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    data = seed(repo)
    sessions = repo.sessions
    reviewer = "google/gemini-decision-1"
    accounts = {"openai": uuid4(), "openrouter": uuid4()}
    with sessions.begin() as db:
        for provider, model in (("openai", openrouter.DIRECT_MODEL), ("openrouter", reviewer)):
            db.add(
                ProviderAccount(
                    id=accounts[provider],
                    provider=provider,
                    name=provider,
                    state="ready",
                    sealed_key="fixture",
                    models=[model],
                    model_catalog={
                        model: {
                            "endpoints": [openrouter.DECISIONS],
                            "questions": ["choice", "binary"],
                        }
                    },
                )
            )
        db.add(ProjectReviewBudget(project_id=data["project"], token_limit=24000))
        initial_links = db.scalar(select(func.count()).select_from(ProjectIssueStory))
    try:
        review_flows.assign(sessions, "falcone", "evidence", reviews.ADVISORY_FLOW, None)
        linked = pending and primary == second == "unrelated"
        search = "Locked issue" if linked else "Append the invocation ledger"
        view = review_evidence.preview(sessions, "falcone", None, "", "", 1, search)
        assert view["model_calls"] == 0 and view["second_model"]["model"] == reviewer
        pair = view["items"][0]
        first = uuid4()
        repeat = uuid4()
        arms = []
        for run in (first, repeat):
            review_evidence.start(sessions, "falcone", run, "", "", 12000, pair["id"])
            for _ in range(7):
                call = review_evidence.advance(sessions, run)
                if not call:
                    continue
                assert run == first
                arms.append(call.arm)
                is_second = call.arm == "second_opinion"
                provider = "openrouter" if is_second else "openai"
                assert call.question.choices == (
                    (provider, reviewer if is_second else openrouter.DIRECT_MODEL),
                )
                assert call.question.system == (
                    decisions.CRITERIA_INSTRUCTIONS if is_second else decisions.LEGACY_ORIGINAL
                )
                assert call.question.native_rubric == (
                    decisions.CRITERIA_RUBRIC if is_second else decisions.DIRECT_RUBRIC
                )
                source = json.loads(call.question.prompt)
                assert all(source[k]["text"] == pair[k]["body"] for k in ("issue", "story"))
                verdict = {
                    "scope": second if is_second else primary,
                    "link_probability": second_link if is_second else link,
                }
                reply = Reply(
                    verdict,
                    "",
                    provider,
                    call.question.choices[0][1],
                    accounts[provider],
                    provider,
                    {"tokens": 21, "input": 21, "output": 0},
                    0.1,
                    1,
                )
                if is_second:
                    assert (
                        review_evidence.control(sessions, "falcone", run, "stop")["state"]
                        == "stopping"
                    )
                review_evidence.settle(sessions, call, reply)
                review_evidence.settle(sessions, call, reply)
                if not is_second and linked:
                    with sessions.begin() as db:
                        db.get_one(ProjectReviewBudget, data["project"]).token_limit = 21
                    assert review_evidence.advance(sessions, run) is None
                    with sessions.begin() as db:
                        held = db.get_one(ReviewEvidenceJob, run)
                        assert held.state == "held" and held.reserved == 0
                        db.get_one(ProjectReviewBudget, data["project"]).token_limit = 24000
                    review_evidence.control(sessions, "falcone", run, "resume")
                if is_second:
                    assert (
                        review_evidence.control(sessions, "falcone", run, "resume")["state"]
                        == "queued"
                    )
            with sessions() as db:
                job = db.get_one(ReviewEvidenceJob, run)
                assert job.state == "complete"
                assert job.spent == (len(arms) * 21 if run == first else 0)
                assert job.result["advisory"]["scope"] == (
                    None if pending and not linked else primary
                )
                assert job.result["advisory"]["existing_link"] == linked
                if run == repeat:
                    assert job.result["repeat_of"] == str(first)
        assert arms == (["decision", "second_opinion"] if second is not None else ["decision"])
        for advisory in (False, True):
            listing = evidence_verdicts.listing(sessions, "falcone", False, 1, advisory)
            assert listing["total"] == int(advisory != pending)
        detail = evidence_verdicts.detail(sessions, "falcone", first)
        assert detail["advisory"]["status"] == ("needs_review" if pending else "advisory")
        assert (
            detail["second_opinion"]["scope"] == second
            if second
            else detail["second_opinion"] is None
        )
        assert detail["story"]["body"] == pair["story"]["body"]
        with sessions() as db:
            budget = db.get_one(ProjectReviewBudget, data["project"])
            assert (budget.used, budget.reserved) == (21 * len(arms), 0)
            assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == len(arms)
            assert db.scalar(select(func.count()).select_from(ProjectIssueStory)) == initial_links
    finally:
        client.close()
