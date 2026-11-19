"""Cross-project work keeps its local ownership, evidence and approvals."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from engineering_control.db.models import (
    BacklogItem,
    ProjectConnection,
    ProviderAccount,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceModelUsage,
)
from engineering_control.models.service import Question, Reply
from tests.test_dashboard_web import _authenticated_client

ROOT = "/api/dashboard/projects/"
F = ROOT + "falcone/connections"
S = ROOT + "falcone-serverless/connections"


def write(client: Any, path: str, **body: Any) -> Any:
    return client.post(
        path, json={"csrf_token": client.get("/api/dashboard/session").json()["csrf_token"], **body}
    )


def add_project(repo: Any) -> UUID:
    return repo.projects.identity_of("falcone-serverless")


def check(repo: Any, model: Any = None) -> None:
    for _ in range(20):
        if not asyncio.run(repo.connection_analysis.tick(model)):
            return
    raise AssertionError("Connection analysis did not finish")


def due(repo: Any, key: str) -> None:
    with repo.sessions.begin() as db:
        db.get(ProjectConnection, UUID(key)).due_at = datetime.now(UTC) - timedelta(seconds=1)


def test_references_resolve_after_import_and_links_survive_review_and_disconnect(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        pid = repo.projects.identity_of("falcone")
        target = add_project(repo)
        ep = UUID(repo.planning.save("falcone", "epics", "Compute", None, [])["id"])
        st = UUID(repo.planning.save("falcone", "stories", "Invoke functions", None, [ep])["id"])
        issue = UUID(
            repo.planning.save(
                "falcone",
                "issues",
                "Migrate task runner",
                "Story gntik-ai/falcone-serverless#32. "
                "Epic https://github.com/gntik-ai/falcone-serverless/issues/6. Other x/y#32.",
                [st],
            )["id"]
        )
        with repo.sessions.begin() as db:
            it = db.get(BacklogItem, issue)
            it.repository, it.issue_number, it.issue_state, it.workspace_review = (
                "gntik-ai/falcone",
                1421,
                "closed",
                "approved",
            )
        assert write(client, F, other="falcone", relation="depends_on").status_code == 422
        assert client.post(
            F, json={"other": "falcone-serverless", "relation": "depends_on"}
        ).status_code in (403, 422)
        assert client.get("/console/workspace/falcone/settings/connections").status_code == 200
        id = write(
            client, F, other="falcone-serverless", relation="depends_on", purpose="Replace Temporal"
        ).json()["id"]
        assert write(client, S, other="falcone", relation="related").status_code == 409
        assert client.get(S).json()["items"][0]["relation"] == "used_by"
        check(repo)
        path = F + "/" + id
        initial = client.get(path).json()
        assert initial["status"] == "waiting" and len(initial["findings"]) == 2
        assert sorted(f["target"]["number"] for f in initial["findings"]) == [6, 32]
        assert all(f["target"]["id"] is None for f in initial["findings"])
        assert client.get(ROOT + "llmwiki/connections/" + id).status_code == 404
        # The shared provider can also support another project.
        shared = write(
            client, ROOT + "llmwiki/connections", other="falcone-serverless", relation="depends_on"
        )
        assert shared.status_code == 200 and len(client.get(S).json()["items"]) == 2
        epic = UUID(repo.planning.save("falcone-serverless", "epics", "Platform", None, [])["id"])
        story = UUID(
            repo.planning.save("falcone-serverless", "stories", "Workflow execution", None, [epic])[
                "id"
            ]
        )
        with repo.sessions.begin() as db:
            for kind, num, key in (("epics", 6, epic), ("stories", 32, story)):
                db.add(
                    WorkspaceCandidate(
                        project_id=target,
                        kind=kind,
                        number=num,
                        local_id=key,
                        identity=f"issue:{num}",
                        decision="approved",
                        title=kind,
                    )
                )
        due(repo, id)
        check(repo)
        data = client.get(path).json()
        assert data["status"] == "needs_model" and {
            f["target"]["id"] for f in data["findings"]
        } == {str(epic), str(story)}
        proposal = data["findings"][0]
        decision = path + "/findings/" + proposal["id"]
        assert write(client, decision, accept=True, version=proposal["version"]).status_code == 200
        assert write(client, decision, accept=True, version=proposal["version"]).status_code == 200
        assert (
            write(
                client,
                path + "/links",
                source_id=str(issue),
                target_kind="epics",
                target_id=str(ep),
            ).status_code
            == 422
        )
        assert write(client, path + "/links", source_id=str(issue)).status_code == 200
        page = client.get(
            ROOT + "falcone/planning/issues?connected=falcone-serverless&search=serverless"
        ).json()
        assert (
            page["total"] == 1
            and page["items"][0]["id"] == str(issue)
            and len(page["items"][0]["connections"]) == 3
        )
        backlink = client.get(
            ROOT + "falcone-serverless/planning/stories?record=" + str(story)
        ).json()
        assert backlink["items"][0]["connections"][0]["record"] == str(issue)
        assert client.get(ROOT + "falcone/planning/issues?connected=llmwiki").json()["total"] == 0
        assert client.get(ROOT + "falcone/planning/counts").json()["issues"] == 1
        remaining = next(f for f in data["findings"] if f["id"] != proposal["id"])
        with repo.sessions.begin() as db:
            item = db.get(BacklogItem, issue)
            item.description = (
                "Revised migration. Story gntik-ai/falcone-serverless#32 "
                "and Epic gntik-ai/falcone-serverless#6."
            )
        assert (
            write(
                client,
                path + "/findings/" + remaining["id"],
                accept=True,
                version=remaining["version"],
            ).status_code
            == 409
        )
        due(repo, id)
        check(repo)
        refreshed = client.get(path).json()["findings"]
        assert len(refreshed) == 3
        assert all(f["status"] == "pending" for f in refreshed if f["origin"] == "reference")
        repo.planning.remove("falcone-serverless", "stories", story)
        due(repo, id)
        check(repo)
        retained = next(
            f for f in client.get(path).json()["findings"] if f["target"]["number"] == 32
        )
        assert retained["target"]["id"] is None and retained["status"] == "pending"
        assert write(client, path + "/disconnect").status_code == 200
        assert client.get(path).json()["archived"] and client.get(path).json()["history"]
        assert client.get(ROOT + "falcone/planning/issues").json()["items"][0]["connections"] == []
        assert repo.projects.default_name() == "falcone" and pid != target
    finally:
        client.__exit__(None, None, None)


def test_model_gaps_resume_after_restart_and_owner_approves_before_publication(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        target = add_project(repo)
        source = repo.projects.identity_of("falcone")
        anchor = repo.planning.save(
            "falcone", "epics", "FaaS orchestration", "Workflow lifecycle", []
        )["id"]
        epic = repo.planning.save(
            "falcone-serverless", "epics", "Execution", "Runtime lifecycle", []
        )["id"]
        parent = repo.planning.save(
            "falcone-serverless", "stories", "Durable jobs", None, [UUID(epic)]
        )["id"]
        with repo.sessions.begin() as db:
            db.add(
                WorkspaceImport(
                    project_id=target,
                    repository="gntik-ai/falcone-serverless",
                    branch="main",
                    stage="complete",
                )
            )
            db.add(
                ProviderAccount(
                    name="fixture",
                    provider="openai",
                    state="ready",
                    sealed_key="test-only",
                    models=["test-model"],
                )
            )
        key = write(
            client, F, other="falcone-serverless", relation="related", purpose="Integration"
        ).json()["id"]
        path = F + "/" + key
        assert (
            write(client, path + "/analyze", provider="openai", model="unknown").status_code == 422
        )
        assert (
            write(client, path + "/analyze", provider="openai", model="test-model").status_code
            == 200
        )
        calls = []

        class Model:
            async def ask(self, q: Question, *, retry: bool) -> Reply:
                assert q.choices == (("openai", "test-model"),)
                ctx = json.loads(q.prompt)
                calls.append(ctx)
                results = []
                if ctx["source"] == "falcone":
                    for action, kind, dest, parents, title in (
                        ("create", "issues", "", [parent], "Implement cancel handler"),
                        ("edit", "epics", epic, [], "Execution lifecycle"),
                        ("link", "stories", parent, [], ""),
                    ):
                        results.append(
                            {
                                "source": anchor,
                                "target": dest,
                                "kind": kind,
                                "action": action,
                                "title": title,
                                "description": "Handle cancellation signals",
                                "parents": parents,
                                "reason": "Both repositories define durable job lifecycle.",
                            }
                        )
                return Reply(
                    {"suggestions": results},
                    "",
                    "openai",
                    "test-model",
                    uuid4(),
                    "fixture",
                    {"tokens": 45},
                    0.1,
                    1,
                )

        lease = repo.connection_analysis.claim()
        assert lease and not repo.connection_analysis.claim()
        due(repo, key)
        check(repo, Model())
        assert len(calls) == 2
        data = client.get(path).json()
        assert data["status"] == "ready" and data["processed"] == data["total"] == 3
        assert len(data["findings"]) == 3
        for f in data["findings"]:
            assert (
                write(
                    client,
                    path + "/findings/" + f["id"],
                    accept=f["action"] != "link",
                    version=f["version"],
                ).status_code
                == 200
            )
        page = client.get(ROOT + "falcone-serverless/planning/issues").json()
        assert page["pending"] == 1 and page["items"][0]["proposal"]
        candidate = page["items"][0]["id"]
        assert (
            client.get(ROOT + "falcone-serverless/planning/epics").json()["items"][0]["title"]
            == "Execution lifecycle"
        )
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 0
            assert set(db.scalars(select(WorkspaceModelUsage.project_id))) == {source}
        assert (
            write(
                client,
                ROOT + "falcone-serverless/planning/issues/decisions",
                ids=[candidate],
                version=None,
            ).status_code
            == 200
        )
        with repo.sessions() as db:
            actions = list(db.scalars(select(WorkspaceAction)))
            assert (
                len(actions) == 1
                and actions[0].operation == "create"
                and actions[0].project_id == target
            )
            assert db.get(WorkspaceImport, target).stage == "complete"
        due(repo, key)
        check(repo, Model())
        existing = len(calls)
        due(repo, key)
        check(repo, Model())
        assert len(calls) == existing and len(client.get(path).json()["findings"]) == 3
    finally:
        client.__exit__(None, None, None)
