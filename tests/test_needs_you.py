"""The worklists say where each answer stands: what needs the administrator, the most
doubtful first, and what two models and the texts agree on, which goes on in a batch or,
where the project's record of judged samples allows it, is applied without the
administrator, with a sample to judge afterwards and a stop."""

from pathlib import Path
from typing import Any

from sqlalchemy import select

from engineering_control.dashboard import parent_choice, reorg
from engineering_control.db.models import BacklogItem, ProjectIssueStory, ProjectRecord
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_judging import Readers
from tests.test_orphans import run
from tests.test_review_settings import BASE, ready

SAVE = {
    "dollar_limit": 5,
    "answer_provider": "fixture",
    "answer_model": "analysis",
    "judge_provider": "fixture",
    "judge_model": "other",
    "makers": ["fixture"],
    "rounds": 0,
}


def candidates(client: Any) -> dict[int, list[str]]:
    """For each open issue with no User Story: its candidates, the texts' first first."""
    listed = client.get(BASE + "orphans").json()["items"]
    return {item["number"]: [entry["title"] for entry in item["shortlist"]] for item in listed}


def placed(repo: Any, number: int) -> bool:
    with repo.sessions() as db:
        issue = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == number)).one()
        return bool(
            db.scalar(
                select(ProjectIssueStory.story_id).where(ProjectIssueStory.item_id == issue.id)
            )
        )


def test_the_worklist_opens_on_what_needs_the_administrator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        shown = candidates(client)
        first, other = shown[41][0], shown[41][1]
        assert shown[42][0] == first
        assert post(client, "orphans/runs").status_code == 200
        run(
            repo,
            Readers(
                {
                    # Both choose the Story the texts put first: settled.
                    ("analysis", 41): [first],
                    ("other", 41): [first],
                    # Both choose the other one: they agree, and the texts do not.
                    ("analysis", 42): [other],
                    ("other", 42): [other],
                    # It cannot tell.
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        listed = client.get(BASE + "orphans").json()
        assert (listed["counts"]["needs"], listed["counts"]["settled"]) == (2, 1)
        needs = client.get(BASE + "orphans", params={"show": "needs"}).json()["items"]
        # The most doubtful first: the answer that could not tell, then the agreement
        # the texts speak against.
        assert [(item["number"], item["review"]["stands"]) for item in needs] == [
            (45, "unclear"),
            (42, "texts"),
        ]
        assert needs[1]["review"]["texts_same"] is False
        assert "texts alone point to another" in needs[1]["review"]["stands_says"]
        (settled,) = client.get(BASE + "orphans", params={"show": "settled"}).json()["items"]
        assert (settled["number"], settled["review"]["texts_same"]) == (41, True)
        with repo.sessions() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            stands = parent_choice.standings(db, project, "issue_story")
            assert (stands["settled"], stands["texts"], stands["unclear"]) == (1, 1, 1)

        # Only what is settled goes into the batch; kept out of batches altogether when
        # the settings say so.
        kinds = {k["kind"]: k for k in client.get(BASE + "reorganize/batches").json()["kinds"]}
        assert kinds["place"]["ready"] == 1 and kinds["place"]["alone"]["setting"] == "propose"
        assert post(client, "review-settings", **SAVE, alone={"place": "off"}).status_code == 200
        kinds = {k["kind"]: k for k in client.get(BASE + "reorganize/batches").json()["kinds"]}
        assert kinds["place"]["ready"] == 0 and kinds["place"]["alone"]["setting"] == "off"
        assert (
            post(client, "review-settings", **SAVE, alone={"nothing": "apply"}).status_code == 422
        )
    finally:
        client.close()


def test_what_is_agreed_is_applied_on_the_record_and_stops_when_the_sample_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        numbers = {"alone_from": 1, "sample_least": 1, "alone_sample": 50}
        asked = {**SAVE, "numbers": numbers, "alone": {"place": "apply"}}
        assert post(client, "review-settings", **asked).status_code == 200
        shown = candidates(client)
        first = shown[41][0]
        assert post(client, "orphans/runs").status_code == 200
        run(
            repo,
            Readers(
                {
                    ("analysis", 41): [first],
                    ("other", 41): [first],
                    ("analysis", 42): ["unclear"],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        # Asked for, and not in force: no sampled line was judged in this project yet.
        standing = client.get(BASE + "review-settings").json()["alone"]
        place = next(entry for entry in standing if entry["kind"] == "place")
        assert (place["setting"], place["in_force"], place["judged"]) == ("apply", False, 0)
        assert "begins after 1" in place["why"]
        assert repo.reorg.apply_all_alone() == 0 and not placed(repo, 41)

        # The administrator judges a batch's sample and approves it: that is the record.
        made = post(client, "reorganize/batches", kind="place")
        assert made.status_code == 200, made.text
        batch = made.json()["id"]
        lines = client.get(BASE + f"reorganize/batches/{batch}").json()["items"]
        (line,) = [entry for entry in lines if entry["sampled"]]
        judged = post(client, f"reorganize/batches/{batch}/lines/{line['id']}", verdict="right")
        assert judged.status_code == 200, judged.text
        assert post(client, f"reorganize/batches/{batch}/approve").status_code == 200
        assert placed(repo, 41)
        with repo.sessions() as db:
            project = db.scalars(
                select(ProjectRecord.id).where(ProjectRecord.name == "falcone")
            ).one()
            standing = reorg.alone_standing(db, project, "place")
            assert (standing["judged"], standing["wrong"], standing["in_force"]) == (1, 0, True)

        # From now on what is settled is applied without the administrator.
        assert post(client, "orphans/runs", again=True).status_code == 200
        run(
            repo,
            Readers(
                {
                    ("analysis", 42): [first],
                    ("other", 42): [first],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        assert not placed(repo, 42)
        assert repo.reorg.apply_all_alone() == 1 and placed(repo, 42)
        past = client.get(BASE + "reorganize/batches").json()["past"]
        applied = past[0]
        assert (applied["alone"], applied["state"], applied["applied"]) == (True, "approved", 1)
        assert (applied["sample"], applied["sample_judged"], applied["can_undo"]) == (1, 0, True)
        with repo.sessions() as db:
            assert reorg.alone_standing(db, project, "place")["waiting"] == 1
        # Nothing more is ready: nothing more is applied.
        assert repo.reorg.apply_all_alone() == 0

        # Its sample is judged afterwards. A line judged wrong is put back, and with one
        # wrong in two the record no longer allows applying: it stops.
        lines = client.get(BASE + f"reorganize/batches/{applied['id']}").json()["items"]
        (line,) = [entry for entry in lines if entry["sampled"]]
        wrong = post(
            client, f"reorganize/batches/{applied['id']}/lines/{line['id']}", verdict="wrong"
        )
        assert wrong.status_code == 200, wrong.text
        assert wrong.json()["undone"] == 1 and not placed(repo, 42)
        place = next(
            entry
            for entry in client.get(BASE + "review-settings").json()["alone"]
            if entry["kind"] == "place"
        )
        assert (place["judged"], place["wrong"], place["in_force"]) == (2, 1, False)
        assert "It stopped" in place["why"]
    finally:
        client.close()
