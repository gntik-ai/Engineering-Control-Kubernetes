from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.dashboard.planning import Kind, PlanningRecords
from engineering_control.dashboard.planning_lifecycle import Operation
from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.db.models import BacklogItem
from tests.test_dashboard_web import _authenticated_client


def confirmed(
    planning: PlanningRecords, name: str, kind: Kind, key: UUID, operation: Operation
) -> dict[str, Any]:
    preview = planning.lifecycle.preview(name, kind, key, operation)
    return planning.lifecycle.confirm(name, kind, key, operation, preview["token"])


def test_project_planning_crud_and_pending_issues(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    base = "/api/dashboard/projects/falcone/planning"
    foreign = "/api/dashboard/projects/llmwiki/planning"
    try:
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def post(path: str, **values: Any) -> Any:
            return client.post(path, json={"csrf_token": csrf, **values})

        assert (
            client.post(base + "/epics", json={"title": "X", "csrf_token": "x" * 40}).status_code
            == 403
        )
        first, second = (
            repository.planning.save("falcone", "epics", name, None, [])["id"]
            for name in ("Catalog", "Billing")
        )
        other = repository.planning.save("llmwiki", "epics", "Independent", None, [])["id"]
        story = post(base + "/stories", title="Issue receipt", epic_ids=[first, second]).json()[
            "id"
        ]
        for invalid in ([], [first, first], [other]):
            assert (
                post(base + f"/stories/{story}/update", title="Bad", epic_ids=invalid).status_code
                == 422
            )

        assert client.get(foreign).json()["stories"] == []
        assert set(client.get(base).json()["stories"][0]["epic_ids"]) == {first, second}
        assert (
            post(foreign + f"/stories/{story}/update", title="Bad", epic_ids=[other]).status_code
            == 404
        )
        assert post(base + f"/epics/{first}/update", title="Catalog v2").status_code == 200
        before = client.get(base + f"/epics/{first}/impact?operation=delete").json()["token"]

        def remove(kind: str, key: str) -> Any:
            url = base + f"/{kind}/{key}"
            token = client.get(url + "/impact?operation=delete").json()["token"]
            return post(url + "/remove", confirmation=token)

        assert remove("epics", second).status_code == 200
        assert post(base + f"/epics/{first}/remove", confirmation=before).status_code == 409
        assert client.get(base).json()["stories"][0]["epic_ids"] == [first]
        assert (
            post(
                base + f"/stories/{story}/update", title="Issue receipt v2", epic_ids=[first]
            ).status_code
            == 200
        )
        more = post(base + "/stories", title="Refunds", epic_ids=[first]).json()["id"]
        draft = post(base + "/issues", title="Add receipt route", story_ids=[story, more])
        assert draft.status_code == 201
        identifier = UUID(draft.json()["id"])
        assert remove("stories", more).status_code == 200
        assert post(base + f"/stories/{story}/remove").status_code == 409
        issue = client.get(base).json()["issues"][0]
        assert issue["story_ids"] == [story]
        assert (issue["number"], issue["url"], issue["review"]) == (None, None, "pending")
        with pytest.raises(ProjectConflict, match="approval"):
            repository.runs.start_item(identifier, toolchain={})
        with repository.sessions() as session:
            assert (
                session.scalar(select(BacklogItem.state).where(BacklogItem.id == identifier))
                == "proposed"
            )
        client.cookies.clear()
        assert client.get(base).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_concurrent_removals_require_a_new_preview_for_changed_children(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        planning = repository.planning
        epics = [
            UUID(planning.save("falcone", "epics", name, None, [])["id"]) for name in ("A", "B")
        ]
        planning.save("falcone", "stories", "One story", None, epics)
        previews = {
            key: planning.lifecycle.preview("falcone", "epics", key, "delete")["token"]
            for key in epics
        }
        together = Barrier(2)

        def remove(epic: UUID) -> bool:
            together.wait(timeout=10)
            try:
                planning.remove("falcone", "epics", epic, previews[epic])
            except ProjectConflict:
                return False
            return True

        with ThreadPoolExecutor(2) as pool:
            assert sum(pool.map(remove, epics)) == 1
        assert len(planning.listing("falcone")["stories"][0]["epic_ids"]) == 1
    finally:
        client.__exit__(None, None, None)
