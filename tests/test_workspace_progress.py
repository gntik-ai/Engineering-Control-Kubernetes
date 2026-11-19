"""Progress remains attached to the project while the administrator signs out."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.workspace_review import _observe_records
from engineering_control.db.models import (
    ProjectRecord,
    WorkspaceAction,
    WorkspaceImport,
    WorkspaceSource,
)
from engineering_control.models import openai_style
from engineering_control.models.service import ModelError, Question, Reply
from engineering_control.models.wire import WireError
from tests.test_dashboard_web import PASSWORD, _authenticated_client, _csrf
from tests.test_workspace_review import ReviewModel

BASE = "/api/dashboard/projects/falcone/"


def seed(repo: DashboardRepository, count: int) -> None:
    with repo.sessions.begin() as db:
        project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        db.add(
            WorkspaceImport(
                project_id=project.id,
                repository=project.source_repository,
                branch="main",
                stage="epics",
                version=1,
                draft="Booking tools",
                model="analysis",
                provider="fixture",
                progress={},
            )
        )
        db.flush()
        for n in range(1, count + 1):
            db.add(
                WorkspaceSource(
                    project_id=project.id,
                    key=f"issue:{n}",
                    kind="issue",
                    evidence={
                        "number": n,
                        "labels": ["epic"],
                        "title": f"Epic {n}",
                        "body": "Booking",
                        "state": "open",
                    },
                )
            )


def post(client: TestClient, path: str, **body: Any) -> httpx.Response:
    csrf = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post(BASE + path, json={"csrf_token": csrf, **body})


def test_background_review_survives_logout_and_counts_whole_candidates(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 10)
    waiting = threading.Event()
    resume = threading.Event()
    batches = []

    class PausingModel(ReviewModel):
        async def ask(self, question: Question, *, retry: bool = True) -> Reply:
            reply = await super().ask(question, retry=retry)
            records = reply.data["records"]
            batches.append([r["number"] for r in records])
            if len(batches) == 2:
                assert question.on_text
                first = '{"records":[' + json.dumps(records[0])
                await question.on_text(first[:-1])
                await question.on_text(first)
                waiting.set()
                assert await asyncio.to_thread(resume.wait, 15)
            return reply

    client.app.state.models = PausingModel()
    try:
        started = post(
            client,
            "import/advance",
            version=1,
            provider="fixture",
            model="analysis",
            background=True,
        )
        assert started.status_code == 200 and waiting.wait(10), client.get(
            BASE + "import?progress_only=true"
        ).text
        status = client.get(BASE + "import?progress_only=true").json()
        assert status["activity"] == "existing" and status["busy"]
        assert (status["checked"], status["found"]) == (9, 9)
        assert "review" not in status and "models" not in status
        assert (
            post(
                client,
                "import/advance",
                version=status["version"],
                provider="fixture",
                model="analysis",
                background=True,
            ).status_code
            == 409
        )
        token = client.get("/api/dashboard/session").json()["csrf_token"]
        assert (
            client.post("/logout", data={"csrf_token": token}, follow_redirects=False).status_code
            == 303
        )
        assert client.get(BASE + "import").status_code == 401
        challenge = client.get("/login/challenge")
        assert (
            client.post(
                "/login",
                data={"csrf_token": _csrf(challenge.text), "password": PASSWORD},
                follow_redirects=False,
            ).status_code
            == 303
        )
        returning = client.get(BASE + "import").json()
        assert returning["busy"] and returning["found"] == 9
        assert len(returning["review"]["candidates"]) == 8
        resume.set()
        for _ in range(100):
            done = client.get(BASE + "import").json()
            if not done["busy"]:
                break
            time.sleep(0.02)
        assert done["operation"] == {"action": "existing", "state": "completed", "added": 10}
        assert done["found"] == done["checked"] == 10 and len(batches) == 2
        assert repo.planning.listing("falcone")["epics"] == []
    finally:
        resume.set()
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_openai_stream_keeps_usage_and_rejects_truncated_answer() -> None:
    pieces = ['{"complete":true,"epics":[', '{"title":"A","description":"one"}', "]}"]
    finish = True
    calls = 0
    snapshots = []

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        chunks = [{"choices": [{"delta": {"content": part}}]} for part in pieces]
        if finish:
            chunks.extend(
                [
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 12}},
                ]
            )
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(
            200, text=content + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"}
        )

    async def observe(text: str) -> None:
        snapshots.append(text)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        options: dict[str, Any] = dict(
            http=http,
            name="Fixture",
            base_url="https://fixture.invalid",
            key="fixture",
            model="m",
            prompt="p",
            system="s",
            schema={},
            max_tokens=100,
            seconds=1,
            retry=False,
            on_text=observe,
        )
        reply = await openai_style.complete(**options)
        assert (reply.read, reply.written, reply.text) == (30, 12, "".join(pieces))
        assert snapshots == ["", pieces[0], pieces[0] + pieces[1], "".join(pieces)]
        finish = False
        with pytest.raises(WireError, match="before it finished"):
            await openai_style.complete(**options)
        assert calls == 2


@pytest.mark.asyncio
async def test_bulk_preview_counts_only_distinct_complete_epics_and_resets_on_failure() -> None:
    progress = []
    observe = _observe_records(
        "epics",
        ["Bookings"],
        set(),
        set(),
        "epics",
        lambda checked, found: progress.append((checked, found)),
    )
    text = '{"epics":['
    for title in ("Bookings", "Reporting", "Reporting", "Audit"):
        record = json.dumps({"title": title, "description": 'Evidence with a } and a "quote".'})
        for ch in record:
            text += ch
            await observe(text)
        text += ","
    assert progress == [(0, 1), (0, 2)]
    await observe("")
    assert progress[-1] == (0, 0)


def test_single_and_bulk_decisions_preserve_other_rows_links_and_outbox(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 2)
    client.app.state.models = ReviewModel()
    try:

        def state() -> dict[str, Any]:
            return client.get(BASE + "import").json()

        def advance(**extra: Any) -> dict[str, Any]:
            result = post(
                client,
                "import/advance",
                version=state()["version"],
                model="analysis",
                provider="fixture",
                **extra,
            )
            assert result.status_code == 200, result.text
            return result.json()

        def decide(kind: str, keys: list[str], **extra: Any) -> httpx.Response:
            return post(
                client, f"planning/{kind}/decisions", ids=keys, version=state()["version"], **extra
            )

        assert (
            client.post(
                BASE + "planning/epics/decisions",
                json={"csrf_token": "x" * 40, "ids": [str(uuid4())]},
            ).status_code
            == 403
        )
        assert post(client, "planning/epics/decisions", ids=[]).status_code == 422
        advance()
        candidates = advance(all_epics=True)["review"]["candidates"]
        first, rest = candidates[0], candidates[1:]
        assert (
            post(
                client,
                "planning/epics/" + first["id"] + "/proposal",
                version=state()["version"],
                title="Edited",
                description="Scope",
                parents=[],
            ).status_code
            == 200
        )
        assert decide("epics", [first["id"], str(uuid4())]).status_code == 404
        assert not repo.planning.listing("falcone")["epics"]
        assert decide("epics", [first["id"]]).status_code == 200
        assert decide("epics", [first["id"]]).status_code == 200
        assert len(repo.planning.listing("falcone")["epics"]) == 1
        assert (
            post(client, "import/review", version=state()["version"], choices=[]).status_code == 409
        )
        assert decide("epics", [rest[-1]["id"]], dismiss=True).status_code == 200
        assert decide("epics", [r["id"] for r in rest[:-1]]).status_code == 200
        assert (
            post(client, "import/review", version=state()["version"], choices=[]).json()["stage"]
            == "stories"
        )
        for kind in ("stories", "issues"):
            advance()
            one = advance()["review"]["candidates"][0]
            assert one["parents"]
            other = repo.planning.save(
                "falcone", kind, "Manually entered", None, [UUID(p) for p in one["parents"]]
            )
            keys = [one["id"], other["id"]] if kind == "issues" else [one["id"]]
            assert decide(kind, keys).status_code == 200
            assert decide(kind, keys).status_code == 200
            rows = repo.planning.listing("falcone")[kind]
            assert all(row["story_ids" if kind == "issues" else "epic_ids"] for row in rows)
            assert (
                post(client, "import/review", version=state()["version"], choices=[]).status_code
                == 200
            )
        with repo.sessions() as db:
            actions = list(db.scalars(select(WorkspaceAction)).all())
            assert [a.operation for a in actions].count("create") == 2
            assert len({(a.operation, a.local_id) for a in actions}) == len(actions)
    finally:
        client.__exit__(None, None, None)


def test_failed_page_resumes_from_its_checkpoint(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 10)
    pages: list[list[int]] = []

    class FailsOnce(ReviewModel):
        async def ask(self, question: Question, *, retry: bool = True) -> Reply:
            pages.append([r["number"] for r in json.loads(question.prompt)["github_issues"]])
            if len(pages) == 2:
                raise ModelError("busy", "Provider temporarily busy")
            return await super().ask(question, retry=retry)

    client.app.state.models = FailsOnce()
    try:

        def run(version: int) -> dict[str, Any]:
            result = post(
                client,
                "import/advance",
                version=version,
                background=True,
                model="analysis",
                provider="fixture",
            )
            assert result.status_code == 200
            for _ in range(100):
                current = client.get(BASE + "import?progress_only=true").json()
                if not current["busy"]:
                    return current
                time.sleep(0.02)
            raise AssertionError("review did not finish")

        failed = run(1)
        assert failed["operation"]["state"] == "failed" and failed["found"] == 8
        assert "temporarily busy" in failed["operation"]["detail"]
        completed = run(failed["version"])
        assert (completed["operation"]["state"], completed["found"], completed["checked"]) == (
            "completed",
            10,
            10,
        )
        assert pages[1] == pages[2] and len(pages) == 3
        assert not set(pages[0]).intersection(pages[2])
    finally:
        client.__exit__(None, None, None)


def test_older_paused_review_and_expired_worker_are_resumable(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 2)
    try:
        with repo.sessions.begin() as db:
            row = db.scalars(select(WorkspaceImport)).one()
            row.progress = {"review_after": "issue:1"}
        paused = client.get(BASE + "import").json()
        assert paused["operation"]["state"] == "interrupted" and paused["checked"] == 1
        with repo.sessions.begin() as db:
            row = db.scalars(select(WorkspaceImport)).one()
            row.lease = uuid4()
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            row.progress = {
                "activity": "all",
                "review_mode": "gaps",
                "found": 3,
                "operation": {"action": "all", "state": "running", "added": 0},
            }
        stopped = client.get(BASE + "import").json()
        assert not stopped["busy"] and stopped["found"] == 0
        assert (
            stopped["operation"]["action"] == "all"
            and stopped["operation"]["state"] == "interrupted"
        )
    finally:
        client.__exit__(None, None, None)
