"""An author approves a clarified, metered Epic description before planning sees it."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from httpx import Response
from sqlalchemy import func, select

from engineering_control.db.models import (
    PlanningReview,
    ProjectEpic,
    ReviewFlowRun,
    WorkspaceModelUsage,
)
from engineering_control.models.service import Question, Reply
from tests.test_dashboard_web import _authenticated_client


class AuthorModel:
    def __init__(self) -> None:
        self.questions: list[Question] = []
        self.answers: list[dict[str, str]] = []

    async def aclose(self) -> None:
        pass

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        self.questions.append(question)
        return Reply(
            self.answers.pop(0),
            "",
            *question.choices[0],
            uuid4(),
            "test account",
            {"tokens": 42},
            0.1,
            1,
        )


def test_clarify_then_accept_exact_project_description_once(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        models = AuthorModel()
        client.app.state.models = models
        repo.planning_analysis.set_model("falcone", "anthropic", "test-model")
        base = "/api/dashboard/projects/falcone/planning/epics"
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        raw = {"title": "Release approval", "description": "Make releases work better."}

        def post(path: str, **data: object) -> Response:
            return client.post(path, json={"csrf_token": csrf, **data})

        models.answers.append(
            {
                "status": "clarification",
                "question": "Who approves a release and what becomes possible?",
                "description": "",
            }
        )
        first = str(uuid4())
        ask = post(base + "/validate", request_id=first, **raw)
        assert ask.status_code == 200, ask.text
        assert ask.json() == {
            "review_id": first,
            "status": "clarification",
            "question": "Who approves a release and what becomes possible?",
        }
        assert post(base + "/validate", request_id=first, **raw).json() == ask.json()
        assert post(base, **raw).status_code == 409
        assert post(base, **raw, review_id=first).status_code == 409
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(ProjectEpic)) == 0
            assert db.scalar(select(func.count()).select_from(PlanningReview)) == 0
        # Simulate an interrupted worker. A retry must never silently repeat inference.
        with repo.sessions.begin() as db:
            run = db.get(ReviewFlowRun, UUID(first))
            run.outcome, run.finished_at = None, None
        assert "already running" in post(base + "/validate", request_id=first, **raw).text
        with repo.sessions.begin() as db:
            run = db.get(ReviewFlowRun, UUID(first))
            run.started_at = datetime.now(UTC) - timedelta(minutes=6)
        assert "interrupted" in post(base + "/validate", request_id=first, **raw).text
        with repo.sessions() as db:
            assert db.get(ReviewFlowRun, UUID(first)).outcome == "failed"
        # Obtain a fresh clarification, as when the author explicitly checks again.
        models.answers.append(
            {"status": "clarification", "question": ask.json()["question"], "description": ""}
        )
        first = str(uuid4())
        assert post(base + "/validate", request_id=first, **raw).json()["status"] == "clarification"
        second = str(uuid4())
        wording = "Release managers can approve a release before it is deployed."
        models.answers.append({"status": "ready", "description": wording, "question": ""})
        reply = post(
            base + "/validate",
            **raw,
            previous=first,
            request_id=second,
            answer="Release managers approve a release before deployment.",
        )
        assert reply.json() == {"review_id": second, "status": "ready", "description": wording}
        assert len(models.questions) == 3 and models.questions[-1].choices == (
            ("anthropic", "test-model"),
        )
        assert (
            "Release managers approve" in models.questions[-1].prompt
            and raw["description"] in models.questions[-1].prompt
        )
        payload = {"title": raw["title"], "description": wording, "review_id": second}
        assert post(base.replace("falcone", "falcone-serverless"), **payload).status_code == 409
        assert post(base, **{**payload, "description": "Changed after checking"}).status_code == 409
        created = post(base, **payload)
        assert created.status_code == 201
        assert post(base, **payload).json() == created.json()
        with repo.sessions() as db:
            assert db.get(ProjectEpic, UUID(created.json()["id"])).description == wording
            assert db.scalar(select(func.count()).select_from(ProjectEpic)) == 1
            assert db.scalar(select(func.count()).select_from(PlanningReview)) == 1
            assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == 3
            run = db.get(ReviewFlowRun, UUID(second))
            assert run.flow_key == "review-epic-intake" and run.outcome == "succeeded"
    finally:
        client.__exit__(None, None, None)


def test_empty_ambiguous_and_unavailable_reviews_do_not_create(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        m = AuthorModel()
        cs = client.get("/api/dashboard/session").json()["csrf_token"]
        url = "/api/dashboard/projects/falcone/planning/epics/validate"

        def check(text: str) -> Response:
            return client.post(
                url,
                json={
                    "title": "Improve releases",
                    "description": text,
                    "csrf_token": cs,
                    "request_id": str(uuid4()),
                },
            )

        assert check("").json()["status"] == "clarification"
        assert check("Improve this").status_code == 409
        repo.planning_analysis.set_model("falcone", "anthropic", "test-model")
        client.app.state.models = m
        m.answers.append(
            {
                "status": "clarification",
                "question": "Which releases?",
                "description": "Invented result",
            }
        )
        assert check("Improve releases").status_code == 422
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(ProjectEpic)) == 0
            assert db.scalar(select(func.count()).select_from(PlanningReview)) == 0
    finally:
        client.__exit__(None, None, None)
