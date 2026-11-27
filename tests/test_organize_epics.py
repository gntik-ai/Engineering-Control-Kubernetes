"""Organize, Epics and User Stories: one button gives every open User Story the one Epic
it belongs to, or puts it before the administrator. It asks which of a Story's Epics it
belongs to, then what its other Epics are to it; a link is taken off only at the
administrator's word, and what was applied can be taken back."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import organize_epics, ranking, review_settings
from engineering_control.db.models import (
    ConnectionFinding,
    PlanningLink,
    ProjectEpic,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ReorgBatch,
    ReorgLine,
    ReviewSettings,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import vector
from tests.test_needs_you import SAVE
from tests.test_orphans import run
from tests.test_project_connections import F, write
from tests.test_review_settings import BASE, ready

HERE = BASE + "organize/epics-stories"
AUDIT, FAILURES = "Audit executions", "Respond to failures"
TRAIL, BILLING, OPERATIONS = "Audit trail", "Billing", "Operations"


class Both:
    """Two models behind one account. Asked which Epic a User Story belongs to, each
    names the Epic it is told for the Story; asked about a Story's other Epics, each says
    of every Epic what it is told, and that it belongs where it is told nothing."""

    def __init__(
        self,
        one: dict[tuple[str, str], str],
        links: dict[tuple[str, str], dict[str, str]] | None = None,
    ) -> None:
        self.one, self.links = one, links or {}
        self.calls: list[tuple[str, str, str]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        ((_provider, model),) = question.choices
        asked = json.loads(question.prompt)
        story = asked["story"]
        said: dict[str, Any]
        if any("judge" in entry for entry in asked["epics"]):
            self.calls.append((model, story["title"], "links"))
            says = self.links.get((model, story["title"]), {})
            said = {
                "story_quote": story["text"],
                "epics": [
                    {
                        "id": entry["id"],
                        "answer": says.get(entry["title"], "part"),
                        "reason": "It reads so.",
                        "epic_quote": entry["text"],
                    }
                    for entry in asked["epics"]
                    if entry["judge"]
                ],
            }
        else:
            self.calls.append((model, story["title"], "one"))
            title = self.one[model, story["title"]]
            if title == "unclear":
                said = {"answer": "unclear", "epic": "", "confidence": "low", "reason": "No."}
                said |= {"story_quote": story["text"], "epic_quote": ""}
            else:
                chosen = next(entry for entry in asked["epics"] if entry["title"] == title)
                said = {
                    "answer": "epic",
                    "epic": chosen["id"],
                    "confidence": "high",
                    "reason": "It reads so.",
                    "story_quote": story["text"],
                    "epic_quote": chosen["text"],
                }
        usage = {"tokens": 40, "cost": 0.001}
        return Reply(said, "", "fixture", model, uuid4(), "Test", usage, 0.1, 1)


def plan(repo: Any) -> dict[str, UUID]:
    """Two open Stories: one under three Epics, the other under two. Their links came
    with a proposal approved as a whole: nobody looked at each."""
    with repo.sessions.begin() as db:
        project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        ids = {
            title: key
            for model in (ProjectEpic, ProjectUserStory)
            for key, title in db.execute(
                select(model.id, model.title).where(model.project_id == project.id)
            )
        }
        trail = ProjectEpic(
            project_id=project.id,
            title=TRAIL,
            description="Durable audit entries of executions.",
        )
        billing = ProjectEpic(
            project_id=project.id, title=BILLING, description="Invoices and payments."
        )
        db.add_all([trail, billing])
        db.flush()
        for story, parent in ((AUDIT, trail), (AUDIT, billing), (FAILURES, trail)):
            db.add(ProjectStoryEpic(project_id=project.id, story_id=ids[story], epic_id=parent.id))
        ids |= {TRAIL: trail.id, BILLING: billing.id, "project": project.id}
    repo.planning_links.reconcile_project("falcone")
    with repo.sessions.begin() as db:
        for link in db.scalars(select(PlanningLink).where(PlanningLink.kind == "story_epic")):
            link.origin, link.confirmed = "model", True
    return ids


def shown(client: Any) -> dict[str, Any]:
    found = client.get(HERE)
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


def lines(client: Any, listed: str) -> list[dict[str, Any]]:
    found = client.get(f"{HERE}/lines/{listed}")
    assert found.status_code == 200, found.text
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def asked(repo: Any, model: Both) -> None:
    """Both checks ask, one after the other, and the run gathers what they answered."""
    for _ in range(4):
        run(repo, model)
        if repo.organize_epics.tick():
            return
    raise AssertionError("The run did not end")


def epics_of(repo: Any, story: UUID) -> set[UUID]:
    with repo.sessions() as db:
        return set(
            db.scalars(select(ProjectStoryEpic.epic_id).where(ProjectStoryEpic.story_id == story))
        )


def one_epic(repo: Any, story: UUID) -> UUID | None:
    """The Epic the administrator's word, or a run in their name, made the Story's own."""
    with repo.sessions() as db:
        found: UUID | None = db.scalar(
            select(PlanningLink.parent_id).where(
                PlanningLink.child_id == story,
                PlanningLink.kind == "story_epic",
                PlanningLink.removed_at.is_(None),
                PlanningLink.is_primary,
                PlanningLink.primary_by == "administrator",
            )
        )
        return found


def answer(client: Any, line: dict[str, Any], choice: str, epic: str | None = None) -> Any:
    body = {"choice": choice, **({"epic_id": epic} if epic else {})}
    return post(client, f"organize/epics-stories/lines/{line['id']}", **body)


AGREE = {
    ("analysis", AUDIT): TRAIL,
    ("other", AUDIT): TRAIL,
    ("analysis", FAILURES): OPERATIONS,
    ("other", FAILURES): TRAIL,
}


def test_one_button_gives_each_story_its_epic_or_puts_it_before_you(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        ids = plan(repo)
        assert post(client, "review-settings", **SAVE).status_code == 200
        before = shown(client)
        # Two open Stories, both under several Epics with none of them chosen.
        assert before["stands"] == {
            "stories": 2,
            "settled": 0,
            "in_doubt": 0,
            "unchecked": 2,
            "left": 0,
            "none": 0,
            "across": 0,
            "epics": 3,
            "epics_with_no_story": 0,
        }
        assert (before["plan"]["to_ask"], before["plan"]["stated"]) == (2, 0)
        assert before["plan"]["answer"]["model"] == "analysis"
        assert before["plan"]["judge"]["model"] == "other"
        assert before["plan"]["applies"]["in_force"] is False
        assert before["can_start"] and before["run"] is None

        assert post(client, "organize/epics-stories/runs").status_code == 200
        under_way = shown(client)["run"]
        assert under_way["state"] == "running"
        assert under_way["asking"] == {
            "about": "the one Epic",
            "done": 0,
            "total": 2,
            "state": "queued",
        }
        assert post(client, "organize/epics-stories/runs").status_code == 409
        assert not repo.organize_epics.tick()
        model = Both(
            AGREE,
            {
                ("analysis", AUDIT): {BILLING: "unrelated", OPERATIONS: "unrelated"},
                ("other", AUDIT): {BILLING: "unrelated", OPERATIONS: "part"},
            },
        )
        asked(repo, model)
        # No Story has its one Epic yet: nobody is asked about a Story's other Epics.
        assert {kind for _model, _story, kind in model.calls} == {"one"}
        after = shown(client)
        first = after["run"]
        assert first["state"] == "complete" and first["asking"] is None and first["cost"] > 0
        assert all(stage["reached"] for stage in first["stages"])
        # Nothing was applied: the project's record does not allow it yet.
        assert after["lists"] == {"needs_you": 2, "done": 0, "created": 0}
        needs = lines(client, "needs_you")
        assert [(line["subject"]["title"], line["case"]) for line in needs] == [
            (FAILURES, "differ"),
            (AUDIT, "settled"),
        ]
        differ, settled = needs
        assert "not applied by itself" in settled["reason"]
        (chosen,) = [entry for entry in settled["candidates"] if entry["chosen"]]
        assert chosen["title"] == TRAIL and chosen["judge"]
        assert [said["by"] for said in settled["said"]] == ["analysis", "other"]
        assert settled["said"][0]["epic"] == TRAIL and settled["said"][0]["passages"]

        # The administrator's word makes it the Story's one Epic; undo takes it back.
        assert answer(client, settled, "epic", str(uuid4())).status_code == 422
        assert answer(client, settled, "take_off").status_code == 422
        assert answer(client, settled, "epic", chosen["id"]).status_code == 200
        assert one_epic(repo, ids[AUDIT]) == ids[TRAIL]
        assert answer(client, settled, "epic", chosen["id"]).status_code == 409
        (done,) = lines(client, "done")
        assert (done["subject"]["title"], done["case"], done["can_undo"]) == (
            AUDIT,
            "one_epic",
            True,
        )
        assert shown(client)["stands"]["settled"] == 1
        assert post(client, f"organize/epics-stories/lines/{done['id']}/undo").status_code == 200
        assert one_epic(repo, ids[AUDIT]) is None and lines(client, "done") == []
        back = next(line for line in lines(client, "needs_you") if line["case"] == "undone")
        assert back["subject"]["title"] == AUDIT and "You took this back" in back["reason"]
        assert answer(client, back, "epic", chosen["id"]).status_code == 200

        # Now that the Story has its one Epic, the next run asks what its other Epics
        # are to it. Nothing is taken off: both links wait for the administrator.
        assert shown(client)["plan"]["links_to_ask"] == 1
        assert post(client, "organize/epics-stories/runs").status_code == 200
        asked(repo, model)
        assert {story for _model, story, kind in model.calls if kind == "links"} == {AUDIT}
        again = shown(client)
        second = again["run"]
        assert again["waiting"] == {"unrelated": 1, "in_doubt": 1}
        # The case that waited from the first run is the same line, not a second one.
        assert again["lists"]["needs_you"] == 3
        assert epics_of(repo, ids[AUDIT]) == {ids[TRAIL], ids[BILLING], ids[OPERATIONS]}
        links = {line["case"]: line for line in lines(client, "needs_you")}
        assert set(links) == {"differ", "link_differ", "link_unrelated"}
        unrelated, link_differ = links["link_unrelated"], links["link_differ"]
        assert [entry["title"] for entry in unrelated["candidates"]] == [BILLING]
        assert [said["answer"] for said in unrelated["said"]] == ["unrelated", "unrelated"]
        assert [said["answer"] for said in link_differ["said"]] == ["unrelated", "part"]

        # One word takes off every link both models call unrelated; a link in doubt
        # cannot be taken off that way.
        many = "organize/epics-stories/groups"
        assert post(client, many, group="in_doubt", choice="take_off").status_code == 422
        assert answer(client, unrelated, "epic", chosen["id"]).status_code == 422
        taken = post(client, many, group="unrelated", choice="take_off")
        assert taken.json() == {"answered": 1, "skipped": 0, "left": 0}
        assert epics_of(repo, ids[AUDIT]) == {ids[TRAIL], ids[OPERATIONS]}
        # Taken back, the link stands again and the case waits; taken off once more.
        off = next(line for line in lines(client, "done") if line["case"] == "link_off")
        assert post(client, f"organize/epics-stories/lines/{off['id']}/undo").status_code == 200
        assert epics_of(repo, ids[AUDIT]) == {ids[TRAIL], ids[BILLING], ids[OPERATIONS]}
        waits = next(line for line in lines(client, "needs_you") if line["case"] == "undone")
        assert "stands again" in waits["reason"]
        assert answer(client, waits, "take_off").status_code == 200
        assert epics_of(repo, ids[AUDIT]) == {ids[TRAIL], ids[OPERATIONS]}

        # Kept, the links in doubt leave the list and are not put before the
        # administrator again.
        kept = post(client, many, group="in_doubt", choice="keep")
        assert kept.json() == {"answered": 1, "skipped": 0, "left": 0}
        # Left as it is, a Story under several Epics is counted as left.
        assert answer(client, differ, "leave").status_code == 200
        assert lines(client, "needs_you") == []
        now = shown(client)
        assert now["stands"]["settled"] == 1 and now["stands"]["left"] == 1
        assert now["plan"]["to_ask"] == 0 and now["plan"]["links_in_doubt"] == 0
        assert post(client, "organize/epics-stories/runs").status_code == 200
        asked(repo, Both({}))
        assert shown(client)["lists"]["needs_you"] == 0

        # Everything a run applied is taken back at once.
        undone = post(client, f"organize/epics-stories/runs/{second['id']}/undo")
        assert undone.json() == {"undone": 1, "kept": 0}
        assert epics_of(repo, ids[AUDIT]) == {ids[TRAIL], ids[BILLING], ids[OPERATIONS]}
        undone = post(client, f"organize/epics-stories/runs/{first['id']}/undo")
        assert undone.json() == {"undone": 1, "kept": 0}
        assert one_epic(repo, ids[AUDIT]) is None
        assert client.get(f"{HERE}/lines/elsewhere").status_code == 422
        assert post(client, f"organize/epics-stories/lines/{uuid4()}/undo").status_code == 404
    finally:
        client.close()


def test_what_is_settled_is_applied_where_the_record_allows_and_a_named_epic_by_rule(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        ids = plan(repo)
        numbers = {"alone_from": 1, "sample_least": 1, "alone_sample": 50}
        saved = post(client, "review-settings", **SAVE, numbers=numbers, alone={"primary": "apply"})
        assert saved.status_code == 200, saved.text
        with repo.sessions.begin() as db:
            # The project's record: one sampled line of a batch was judged right.
            batch = ReorgBatch(project_id=ids["project"], kind="primary", state="approved")
            db.add(batch)
            db.flush()
            db.add(
                ReorgLine(
                    batch_id=batch.id,
                    project_id=ids["project"],
                    link_kind="story_epic",
                    child_id=uuid4(),
                    parent_id=uuid4(),
                    confidence=1.0,
                    reason="Judged before.",
                    sampled=True,
                    verdict="right",
                    state="skipped",
                )
            )
            # The text of one Story names one of its Epics.
            named = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.child_id == ids[FAILURES],
                    PlanningLink.parent_id == ids[OPERATIONS],
                )
            ).one()
            named.origin = "declared"
        before = shown(client)
        assert before["plan"]["applies"]["in_force"] is True
        # The Story that names its Epic is not asked about.
        assert (before["plan"]["to_ask"], before["plan"]["stated"]) == (1, 1)
        assert post(client, "organize/epics-stories/runs").status_code == 200
        # By rule, at once: the Epic its own text names is its one Epic.
        assert one_epic(repo, ids[FAILURES]) == ids[OPERATIONS]
        (ruled,) = lines(client, "done")
        assert ruled["subject"]["title"] == FAILURES and "own text names" in ruled["reason"]
        model = Both({("analysis", AUDIT): TRAIL, ("other", AUDIT): TRAIL})
        asked(repo, model)
        assert ("analysis", FAILURES, "one") not in model.calls
        # What both models agree on is applied without the administrator.
        assert one_epic(repo, ids[AUDIT]) == ids[TRAIL]
        after = shown(client)
        assert after["lists"] == {"needs_you": 0, "done": 2, "created": 0}
        assert (after["stands"]["settled"], after["stands"]["unchecked"]) == (2, 0)
        stages = {stage["stage"]: stage for stage in after["run"]["stages"]}
        assert (stages["verify"]["settled"], stages["verify"]["in_doubt"]) == (2, 0)
        done = {line["subject"]["title"]: line for line in lines(client, "done")}
        assert all(line["can_undo"] and line["case"] == "one_epic" for line in done.values())
        assert done[AUDIT]["candidates"][0]["title"] == TRAIL

        # Taken back, each stands as before; the applied one counts as judged wrong.
        for line in done.values():
            undone = post(client, f"organize/epics-stories/lines/{line['id']}/undo")
            assert undone.status_code == 200, undone.text
        assert one_epic(repo, ids[AUDIT]) is None and one_epic(repo, ids[FAILURES]) is None
        with repo.sessions() as db:
            judged = [
                line.verdict
                for line in db.scalars(select(ReorgLine).where(ReorgLine.state == "undone"))
            ]
        assert judged == ["wrong"]
        waiting = lines(client, "needs_you")
        assert {line["case"] for line in waiting} == {"undone"} and len(waiting) == 2
    finally:
        client.close()


def meaning(
    repo: Any, ids: dict[str, UUID], values: dict[str, tuple[str, tuple[float, ...]]]
) -> None:
    with repo.sessions.begin() as db:
        for title, (kind, numbers) in values.items():
            ranking.keep_vector(db, ids["project"], kind, ids[title], "m", "d", vector(*numbers))


def test_an_only_epic_both_rankings_place_elsewhere_and_a_story_with_none(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        ids = plan(repo)
        assert post(client, "review-settings", **SAVE).status_code == 200
        refunds = repo.planning.save(
            "falcone",
            "stories",
            "Refund a payment",
            "Refund invoices and payments.",
            [ids[OPERATIONS]],
        )["id"]
        lonely = repo.planning.save(
            "falcone",
            "stories",
            "Refresh dependencies",
            "Keep dependencies current, with durable entries.",
            [ids[TRAIL]],
        )["id"]
        with repo.sessions.begin() as db:
            # Its only Epic comes off: the Story stands under none.
            for link in db.scalars(
                select(ProjectStoryEpic).where(ProjectStoryEpic.story_id == UUID(lonely))
            ):
                db.delete(link)
        ids |= {"Refund a payment": UUID(refunds), "Refresh dependencies": UUID(lonely)}
        # Without the ranking service nothing calls an only Epic in question.
        assert shown(client)["plan"]["only"] == 1
        meaning(
            repo,
            ids,
            {
                "Refund a payment": ("story", (1, 0, 0)),
                BILLING: ("epic", (1, 0, 0)),
                TRAIL: ("epic", (0.8, 0.6, 0)),
                OPERATIONS: ("epic", (0, 0, 1)),
            },
        )
        # A connected project holds the Epic the Story with none is about.
        upkeep = repo.planning.save(
            "falcone-serverless", "epics", "Upkeep", "Keep dependencies current.", []
        )["id"]
        connected = write(client, F, other="falcone-serverless", relation="depends_on")
        assert connected.status_code == 200, connected.text
        organize_epics._FLAGS.clear()
        before = shown(client)
        assert before["stands"]["none"] == 1 and before["plan"]["only"] == 2
        assert post(client, "organize/epics-stories/runs").status_code == 200
        asked(repo, Both({**AGREE, ("other", FAILURES): OPERATIONS}))
        needs = {line["case"]: line for line in lines(client, "needs_you")}
        wrong, none = needs["only_epic"], needs["no_epic"]
        # Both the words and the meaning place Billing first, and Operations last.
        assert wrong["subject"]["title"] == "Refund a payment"
        assert "No model was asked" in wrong["reason"]
        assert [(e["title"], e["current"]) for e in wrong["candidates"]] == [
            (BILLING, False),
            (TRAIL, False),
            (OPERATIONS, True),
        ]
        current = wrong["candidates"][-1]["id"]
        assert answer(client, wrong, "epic", current).status_code == 422
        assert answer(client, wrong, "epic", wrong["candidates"][0]["id"]).status_code == 200
        # Moved: it stands under Billing alone, as its one Epic.
        assert epics_of(repo, UUID(refunds)) == {ids[BILLING]}
        moved = next(line for line in lines(client, "done") if line["answer"] == "epic")
        assert post(client, f"organize/epics-stories/lines/{moved['id']}/undo").status_code == 200
        assert epics_of(repo, UUID(refunds)) == {ids[OPERATIONS]}
        kept = next(line for line in lines(client, "needs_you") if line["case"] == "undone")
        assert answer(client, kept, "keep").status_code == 200

        # The Story with none is offered Epics of the scope, each with its project.
        offered = {entry["title"]: entry for entry in none["candidates"]}
        assert offered["Upkeep"]["project"] == "falcone-serverless"
        assert "project" not in offered[TRAIL]
        assert answer(client, none, "epic", upkeep).status_code == 200
        with repo.sessions() as db:
            (finding,) = db.scalars(select(ConnectionFinding))
            assert (finding.status, finding.origin) == ("approved", "manual")
            assert (finding.source_id, str(finding.target_id)) == (UUID(lonely), upkeep)
        now = shown(client)
        assert (now["stands"]["none"], now["stands"]["across"]) == (0, 1)
        # Kept under its Epic, the other Story is not called in question again.
        assert now["plan"]["only"] == 0
        linked = next(line for line in lines(client, "done") if line["action"].get("across"))
        assert post(client, f"organize/epics-stories/lines/{linked['id']}/undo").status_code == 200
        with repo.sessions() as db:
            assert db.scalars(select(ConnectionFinding.status)).one() == "dismissed"
        assert shown(client)["stands"]["none"] == 1
    finally:
        client.close()


def test_a_run_can_be_stopped_and_gathers_what_needs_no_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        ids = plan(repo)
        # No model is set to answer, and nothing waits that needs none.
        nothing = shown(client)
        assert not nothing["can_start"] and "answer" in nothing["why_not"].lower()
        assert post(client, "organize/epics-stories/runs").status_code == 409
        assert post(client, "review-settings", **SAVE).status_code == 200
        assert post(client, "organize/epics-stories/control", action="stop").status_code == 409
        assert post(client, "organize/epics-stories/runs").status_code == 200
        # Stopped, the run is held; resumed, it asks on; ended, it keeps what it has.
        assert post(client, "organize/epics-stories/control", action="stop").status_code == 200
        held = shown(client)["run"]
        assert held["state"] == "held" and "stopped" in held["detail"].lower()
        assert post(client, "organize/epics-stories/control", action="resume").status_code == 200
        assert shown(client)["run"]["state"] == "running"
        assert post(client, "organize/epics-stories/control", action="end").status_code == 200
        ended = shown(client)
        assert ended["run"]["state"] == "stopped" and ended["can_start"]
        assert "2 User Stories under several Epics have no answer yet" in ended["run"]["detail"]
        assert ended["lists"]["needs_you"] == 0

        # One Story is answered; then no model is set any more.
        assert post(client, "organize/epics-stories/runs").status_code == 200
        asked(repo, Both({**AGREE, ("analysis", FAILURES): "unclear"}))
        assert shown(client)["lists"]["needs_you"] == 2
        with repo.sessions.begin() as db:
            story = db.get_one(ProjectUserStory, ids[AUDIT])
            story.description = "Keep a durable audit entry for each execution, once."
            for settings in db.scalars(
                select(ReviewSettings).where(ReviewSettings.project_id == ids["project"])
            ):
                settings.answer_provider = settings.answer_model = None
        review_settings.forget(ids["project"])
        now = shown(client)
        # It says that nobody is asked and why, and still gathers what waits.
        assert now["plan"]["to_ask"] == 1 and "No model" in now["plan"]["ask_blocked"]
        assert now["can_start"] and now["why_not"] is None
        assert post(client, "organize/epics-stories/runs").status_code == 200
        after = shown(client)
        assert after["run"]["state"] == "complete" and after["run"]["asking"] is None
        assert "1 User Stories under several Epics have no answer yet" in after["run"]["detail"]
        assert after["lists"]["needs_you"] == 1
    finally:
        client.close()
