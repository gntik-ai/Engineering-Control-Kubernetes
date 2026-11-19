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
from engineering_control.dashboard.workspace_issues import import_issues
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


def test_generate_all_stories_checkpoints_and_preserves_approval(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 1)
    epic = repo.planning.save("falcone", "epics", "Bookings", None, [])["id"]
    other = repo.planning.save("falcone", "epics", "Reporting", None, [])["id"]
    repo.planning.save("falcone", "stories", "Manual story", None, [UUID(epic)])
    with repo.sessions.begin() as db:
        db.scalars(select(WorkspaceImport)).one().stage = "stories"
        source = db.scalars(select(WorkspaceSource)).one()
        source.evidence = {**source.evidence, "labels": ["story"], "title": "Imported story"}

    def story(title: str, *parents: str) -> dict[str, Any]:
        return {
            "title": title,
            "description": "As a customer I can review my bookings.",
            "parents": list(parents),
        }

    waiting = threading.Event()
    resume = threading.Event()
    replies = [
        {
            "complete": False,
            "stories": [
                story("Manual story", epic),
                story("Imported story", other),
                story("Export reports", other),
                story("Booking history", epic, other),
                story("BOOKING HISTORY", epic),
                story("Account preferences", epic),
            ],
        },
        {"complete": True, "stories": [story("Monthly report", other)]},
        {"complete": True, "stories": []},
    ]

    class StoryModel(ReviewModel):
        async def ask(self, question: Question, *, retry: bool = True) -> Reply:
            if "stories" not in question.schema["properties"]:
                return await super().ask(question, retry=retry)
            assert question.choices == (("fixture", "analysis"),) and not retry
            ctx = json.loads(question.prompt)
            assert {p["id"] for p in ctx["parents"]} == {epic, other}
            assert {"Manual story", "Export reports", "Imported story"} <= set(ctx["existing"])
            answer = replies.pop(0)
            if len(replies) == 1:
                assert {"Booking history", "Account preferences"} <= set(ctx["existing"])
                assert question.on_text
                await question.on_text(json.dumps(answer))
                waiting.set()
                assert await asyncio.to_thread(resume.wait, 15)
            return Reply(
                answer, "", "fixture", "analysis", uuid4(), "Fixture", {"tokens": 10}, 0.1, 1
            )

    client.app.state.models = StoryModel()
    args = {"provider": "fixture", "model": "analysis"}
    try:
        assert (
            post(client, "import/advance", version=1, all_stories=True, **args).status_code == 409
        )
        reviewed = post(client, "import/advance", version=1, **args).json()
        assert reviewed["review"]["mode"] == "gaps"
        single = post(client, "import/advance", version=reviewed["version"], **args).json()
        version = single["version"]
        assert (
            post(client, "import/advance", version=version, all_epics=True, **args).status_code
            == 409
        )
        started = post(
            client, "import/advance", version=version, all_stories=True, background=True, **args
        )
        assert started.status_code == 200 and waiting.wait(10)
        running = client.get(BASE + "import").json()
        assert running["activity"] == "all" and running["busy"] and running["found"] == 5
        assert running["operation"] == {"action": "all", "state": "running", "added": 2}
        assert (
            post(
                client, "import/advance", version=running["version"], all_stories=True, **args
            ).status_code
            == 409
        )
        resume.set()
        for _ in range(100):
            done = client.get(BASE + "import").json()
            if not done["busy"]:
                break
            time.sleep(0.02)
        assert done["operation"] == {"action": "all", "state": "completed", "added": 3}
        candidates = {c["title"]: c for c in done["review"]["candidates"]}
        assert len(candidates) == 5 and done["review"]["mode"] == "review"
        assert set(candidates["Booking history"]["parents"]) == {epic, other}
        assert candidates["Monthly report"]["parents"] == [other]
        assert all(c["decision"] == "pending" for c in candidates.values())
        assert [s["title"] for s in repo.planning.listing("falcone")["stories"]] == ["Manual story"]
        assert client.get(BASE + "synchronization").json()["items"] == []
        repeated = post(
            client, "import/advance", version=done["version"], all_stories=True, **args
        ).json()
        assert repeated["review"] == done["review"] and repeated["operation"]["added"] == 0
        assert len(replies) == 1, "a completed story batch must not call the model again"
        approved = post(
            client,
            "planning/stories/decisions",
            version=repeated["version"],
            ids=[candidates["Booking history"]["id"]],
        )
        assert approved.status_code == 200
        saved = next(
            s
            for s in repo.planning.listing("falcone")["stories"]
            if s["title"] == "Booking history"
        )
        assert set(saved["epic_ids"]) == {epic, other}
        state = client.get(BASE + "import").json()
        assert state["stage"] == "stories"
        remaining = [c["id"] for c in state["review"]["candidates"] if c["decision"] == "pending"]
        assert (
            post(
                client, "planning/stories/decisions", version=state["version"], ids=remaining
            ).status_code
            == 200
        )
        assert client.get(BASE + "import").json()["stage"] == "issues"
    finally:
        resume.set()
        client.__exit__(None, None, None)


def test_bulk_story_retry_preserves_earlier_batch_and_validates_epics(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 0)
    epic = repo.planning.save("falcone", "epics", "Bookings", None, [])["id"]
    other = repo.planning.save("falcone", "epics", "Reports", None, [])["id"]
    with repo.sessions.begin() as db:
        row = db.scalars(select(WorkspaceImport)).one()
        row.stage = "stories"
        row.progress = {"review_mode": "gaps"}

    def story(title: str, parent: str) -> dict[str, Any]:
        return {"title": title, "description": title + " for users", "parents": [parent]}

    batches = [
        [story("Browse bookings", epic)],
        [story("Invalid link", str(uuid4()))],
        [story("Browse bookings", epic)],
        [story("Download report", other), story("Saved concurrently", other)],
    ]

    class Model(ReviewModel):
        async def ask(self, question: Question, *, retry: bool = True) -> Reply:
            stories = batches.pop(0)
            if not batches:
                repo.planning.save("falcone", "stories", "Saved concurrently", None, [UUID(other)])
            return Reply(
                {"complete": True, "stories": stories},
                "",
                "fixture",
                "analysis",
                uuid4(),
                "Fixture",
                {},
                0.1,
                1,
            )

    client.app.state.models = Model()

    def generate() -> httpx.Response:
        version = client.get(BASE + "import").json()["version"]
        return post(
            client,
            "import/advance",
            version=version,
            provider="fixture",
            model="analysis",
            all_stories=True,
        )

    try:
        assert generate().status_code == 422
        saved = client.get(BASE + "import").json()
        assert saved["operation"]["state"] == "failed" and saved["operation"]["added"] == 1
        assert [c["title"] for c in saved["review"]["candidates"]] == ["Browse bookings"]
        assert generate().status_code == 409
        done = generate().json()
        assert done["operation"] == {"action": "all", "state": "completed", "added": 1}
        assert {c["title"] for c in done["review"]["candidates"]} == {
            "Browse bookings",
            "Download report",
        }
        assert not batches
    finally:
        client.__exit__(None, None, None)


def test_bulk_story_generation_has_a_finite_pass_even_when_model_always_wants_more(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 0)
    with repo.sessions.begin() as db:
        row = db.scalars(select(WorkspaceImport)).one()
        row.stage = "stories"
        row.progress = {"review_mode": "gaps"}
    for i in range(9):
        repo.planning.save("falcone", "epics", f"Epic {i}", None, [])
    calls = []

    class OpenEnded(ReviewModel):
        async def ask(self, q: Question, *, retry: bool = True) -> Reply:
            ctx = json.loads(q.prompt)
            group = ctx["epics_for_this_batch"]
            calls.append(group)
            assert len(calls) <= 4, "one evidence pass and at most one overflow pass per Epic group"
            stories = [
                {
                    "title": f"Outcome {key} pass {len(calls)}",
                    "description": "As a user I can finish the task.",
                    "parents": [key],
                }
                for key in group
            ]
            return Reply(
                {"complete": False, "stories": stories},
                "",
                "fixture",
                "analysis",
                uuid4(),
                "Fixture",
                {},
                0.1,
                1,
            )

    client.app.state.models = OpenEnded()
    try:
        state = post(
            client,
            "import/advance",
            version=1,
            all_stories=True,
            provider="fixture",
            model="analysis",
        ).json()
        assert (
            len(calls) == 4 and state["stage"] == "stories" and state["review"]["mode"] == "review"
        )
        assert state["operation"]["state"] == "completed"
        records = state["review"]["candidates"]
        first = records[0]["parents"][0]
        rejected = [r["id"] for r in records if r["parents"] == [first]]
        assert (
            post(
                client,
                "planning/stories/decisions",
                ids=rejected,
                dismiss=True,
                version=state["version"],
            ).status_code
            == 200
        )
        current = client.get(BASE + "import").json()
        assert current["stage"] == "stories" and current["review"]["mode"] == "gaps"
    finally:
        client.__exit__(None, None, None)


def test_legacy_completed_bulk_stories_advance_without_new_model_usage(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 0)
    ep = repo.planning.save("falcone", "epics", "Existing Epic", None, [])
    repo.planning.save("falcone", "stories", "Approved outcome", None, [UUID(ep["id"])])
    with repo.sessions.begin() as db:
        row = db.scalars(select(WorkspaceImport)).one()
        row.stage = "stories"
        row.progress = {
            "review_mode": "review",
            "activity": "all",
            "operation": {"action": "all", "state": "completed", "added": 0},
        }
    try:
        repo.workspace_imports.settle_existing_stories()
        repo.workspace_imports.settle_existing_stories()
        current = client.get(BASE + "import").json()
        assert current["stage"] == "issues" and current["review"]["mode"] == "existing"
        assert current["version"] == 2
    finally:
        client.__exit__(None, None, None)


def test_generate_all_issues_checks_each_story_and_leaves_local_drafts(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 0)
    epic = UUID(repo.planning.save("falcone", "epics", "Bookings", None, [])["id"])
    stories = sorted(
        repo.planning.save("falcone", "stories", f"Booking outcome {i}", None, [epic])["id"]
        for i in range(9)
    )
    repo.planning.save("falcone", "issues", "Existing work", None, [UUID(stories[0])])
    finished = repo.planning.save("falcone", "stories", "Finished work", None, [epic])["id"]
    repo.planning.set_state("falcone", "stories", UUID(finished), "closed")
    with repo.sessions.begin() as db:
        row = db.scalars(select(WorkspaceImport)).one()
        row.stage = "issues"
        import_issues(
            db,
            db.get(ProjectRecord, row.project_id),
            [
                {"number": n, "state": s, "title": f"GitHub task {n}", "labels": [], "body": ""}
                for n, s in ((31, "open"), (32, "closed"))
            ],
        )
    started, resume = threading.Event(), threading.Event()
    batches = []

    class Model(ReviewModel):
        async def ask(self, q: Question, *, retry: bool = True) -> Reply:
            if "records" in q.schema["properties"]:
                return await super().ask(q, retry=retry)
            assert not retry and q.choices == (("fixture", "analysis"),)
            ctx = json.loads(q.prompt)
            group = ctx["stories_for_this_batch"]
            batches.append(group)
            assert {"Existing work", "GitHub task 31", "GitHub task 32"} <= set(ctx["existing"])
            assert finished not in {p["id"] for p in ctx["parents"]}
            issues = [
                {
                    "title": "Implement " + p,
                    "description": "Test the booking contract.",
                    "parents": [p],
                }
                for p in group
            ]
            if len(batches) == 1:
                assert {"title": "Existing work", "parents": [stories[0]]} in ctx[
                    "saved_issue_links"
                ]
                issues.extend([issues[0], {**issues[0], "title": "Existing work"}])
            answer = {"issues": issues, "complete": True}
            if len(batches) == 2:
                assert q.on_text
                await q.on_text(json.dumps(answer))
                started.set()
                assert await asyncio.to_thread(resume.wait, 15)
            return Reply(answer, "", "fixture", "analysis", uuid4(), "Fixture", {}, 0.1, 1)

    client.app.state.models = Model()
    args = {"provider": "fixture", "model": "analysis", "all_issues": True}
    try:
        assert post(client, "import/advance", version=1, **args).status_code == 409
        assert post(client, "import/advance", version=1, all_epics=True, **args).status_code == 422
        reviewed = post(
            client, "import/advance", version=1, provider="fixture", model="analysis"
        ).json()
        running = post(
            client, "import/advance", version=reviewed["version"], background=True, **args
        )
        assert running.status_code == 200 and started.wait(10)
        progress = client.get(BASE + "import?progress_only=true").json()
        assert progress["busy"] and progress["activity"] == "all"
        assert progress["found"] == 11 and progress["run_found"] == 9
        assert (
            post(client, "import/advance", version=progress["version"], **args).status_code == 409
        )
        resume.set()
        for _ in range(100):
            state = client.get(BASE + "import").json()
            if not state["busy"]:
                break
            time.sleep(0.02)
        assert batches == [stories[:8], stories[8:]]
        assert state["operation"] == {"action": "all", "state": "completed", "added": 9}
        assert state["review"]["bulk_complete"] and not state["review"]["can_continue"]
        candidates = state["review"]["candidates"]
        assert {c["number"] for c in candidates if c["number"]} == {31, 32}
        drafts = [c for c in candidates if c["number"] is None]
        assert all(c["number"] is None and c["decision"] == "pending" for c in drafts)
        assert {c["parents"][0] for c in drafts} == set(stories)
        repeated = post(client, "import/advance", version=state["version"], **args).json()
        assert repeated["review"] == state["review"] and repeated["operation"]["added"] == 0
        assert (
            post(
                client,
                "import/advance",
                version=repeated["version"],
                provider="fixture",
                model="analysis",
            ).status_code
            == 409
        )
        assert len(batches) == 2
        assert client.get(BASE + "synchronization").json()["items"] == []
        last = next(c for c in drafts if c["parents"] == [stories[-1]])
        dismissed = post(
            client,
            "planning/issues/decisions",
            ids=[last["id"]],
            dismiss=True,
            version=repeated["version"],
        )
        assert dismissed.status_code == 200
        state = client.get(BASE + "import").json()
        assert not state["review"]["bulk_complete"] and state["review"]["mode"] == "gaps"
    finally:
        resume.set()
        client.__exit__(None, None, None)


def test_bulk_issues_preserve_partial_batches_and_can_resume(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo, 0)
    epic = UUID(repo.planning.save("falcone", "epics", "Operations", None, [])["id"])
    story = repo.planning.save("falcone", "stories", "Inspect logs", None, [epic])["id"]
    with repo.sessions.begin() as db:
        row = db.scalars(select(WorkspaceImport)).one()
        row.stage, row.progress = "issues", {"review_mode": "gaps"}
    calls = []

    class Model(ReviewModel):
        async def ask(self, q: Question, *, retry: bool = True) -> Reply:
            ctx = json.loads(q.prompt)
            assert set(calls) <= set(ctx["existing"])
            title = f"Add log adapter {len(calls)}"
            calls.append(title)
            answer = {
                "complete": len(calls) == 4,
                "issues": [
                    {"title": title, "description": "Test log ingestion.", "parents": [story]}
                ],
            }
            return Reply(answer, "", "fixture", "analysis", uuid4(), "Fixture", {}, 0.1, 1)

    client.app.state.models = Model()
    args = {"provider": "fixture", "model": "analysis", "all_issues": True}
    try:
        assert post(client, "import/advance", version=1, **args).status_code == 409
        paused = client.get(BASE + "import").json()
        assert paused["operation"]["state"] == "failed" and paused["operation"]["added"] == 3
        assert set(calls) == {c["title"] for c in paused["review"]["candidates"]}
        assert not paused["review"]["bulk_complete"]
        done = post(client, "import/advance", version=paused["version"], **args).json()
        assert done["review"]["bulk_complete"] and done["operation"]["added"] == 1
        assert len(done["review"]["candidates"]) == len(calls) == 4
        assert client.get(BASE + "synchronization").json()["items"] == []
    finally:
        client.__exit__(None, None, None)
