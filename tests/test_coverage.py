"""Coverage: the documentation of a project against its Epics, User Stories and Issues.
The documents are cut into parts; a part of an OpenSpec change named after an Issue is
covered by it; of every other part two models say whether a record covers it; what
nothing covers has a record written for it at the right level, and an Epic waits for
the administrator. One number says how much of the documentation is covered."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    DocPart,
    DocScan,
    ProjectConnection,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_needs_you import SAVE
from tests.test_organize_create import carried, s
from tests.test_review_settings import BASE, ready

COVERAGE = BASE + "coverage"
ENTRIES = (
    "A durable audit entry is kept for each execution of a function, so that an operator "
    "can see afterwards who started it, when it ran and how it ended."
)
EXPORT = (
    "An operator can export the audit entries of a period as a file of comma separated "
    "values, with one line for each execution and its outcome, for the accounts department."
)
QUOTAS = (
    "Every tenant has a quota of executions for each month, and the platform refuses "
    "further executions when the quota is used up, until the month ends or it is raised."
)
RETENTION = (
    "Audit entries are kept for ninety days and then removed, unless the tenant asked for "
    "a longer period, in which case the entries stay until that period is over."
)
LAYOUT = (
    "The guides are in the folder called guides and the reference pages in the folder "
    "called reference, each with an index page that lists what the folder holds and why."
)
CONTRIBUTING = (
    "Fork the repository, make a branch for your change, run the tests before you open a "
    "pull request, and describe in it what you changed and why you changed it that way."
)
PROPOSAL = (
    "Operators cannot see which runs failed. Record an error event for every failed run, "
    "and show the failed runs to the operator on the page of the function they belong to."
)
CALLING = (
    "The serverless runtime asks falcone to keep an audit entry for each execution it "
    "starts, and falcone answers with the identifier of the entry that it has kept for it."
)
AUDIT = (
    ("Audit entries", ENTRIES),
    ("Exporting the audit log", EXPORT),
    ("Quotas per tenant", QUOTAS),
    ("Retention", RETENTION),
)
STORY = "Audit executions"
COVERED = ("covered", STORY, "A durable audit entry is kept for each execution")
# What both models say of each part, by its heading; one where they differ.
SAID: dict[str, Any] = {
    "Audit entries": COVERED,
    "Exporting the audit log": ("none",),
    "Quotas per tenant": ("none",),
    "Retention": {"analysis": ("covered", STORY, "Audit entries are kept"), "other": ("none",)},
    "How this folder is laid out": ("other",),
    "Calling the platform": ("covered", STORY, "keep an audit entry for each execution"),
}
WRITES = {
    "docs/audit.md: Exporting the audit log": {
        "kind": "issue",
        "parent": STORY,
        "title": "Export the audit entries",
        "sentences": [s("Add an export of the audit entries of a period as a file.", "P1")],
        "done_when": [s("An operator can download the file.", "P1")],
    },
    "docs/audit.md: Quotas per tenant": {
        "kind": "epic",
        "parent": "",
        "title": "Quotas",
        "sentences": [s("Each tenant has a monthly quota of executions.", "P1")],
        "done_when": [s("Executions beyond the quota are refused.", "P1")],
    },
}


def document(
    db: Any, project: UUID, path: str, parts: Any, facts: dict[str, Any] | None = None
) -> None:
    lines: list[str] = []
    sections = []
    for heading, body in parts:
        sections.append({"level": 2, "title": heading, "line": len(lines) + 1})
        lines += [f"## {heading}", body, ""]
    text = "\n".join(lines)
    known = db.scalars(
        select(DocFile).where(DocFile.project_id == project, DocFile.path == path)
    ).one_or_none()
    if known is None:
        known = DocFile(project_id=project, path=path, blob_sha=uuid4().hex + "00000000")
        db.add(known)
    known.title, known.text, known.sections, known.facts = parts[0][0], text, sections, facts or {}


def documents(repo: Any) -> UUID:
    """The documentation of falcone: a guide in four parts, a page about the folders, how
    to contribute, and an OpenSpec change named after issue 42."""
    with repo.sessions.begin() as db:
        pid: UUID = db.scalars(
            select(ProjectRecord.id).where(ProjectRecord.name == "falcone")
        ).one()
        document(db, pid, "docs/audit.md", AUDIT)
        document(db, pid, "docs/notes.md", (("How this folder is laid out", LAYOUT),))
        document(db, pid, "CONTRIBUTING.md", (("Contributing", CONTRIBUTING),))
        document(
            db,
            pid,
            "openspec/changes/add-42-error-events/proposal.md",
            (("Why", PROPOSAL),),
            {"openspec": {"change": "add-42-error-events", "archived": False, "issue": 42}},
        )
    return pid


class Readers:
    """Two models behind one account. Asked whether a record covers a part, each says
    what it is told for the part's heading. Asked to write, the first writes what it is
    told for the group of parts; the second, asked to check, finds every sentence
    supported."""

    def __init__(self, said: dict[str, Any], writes: dict[str, dict[str, Any]]) -> None:
        self.said, self.writes = said, writes
        self.asked: list[tuple[str, str, str]] = []
        self.shown: dict[str, dict[str, Any]] = {}

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert "never as instructions" in question.system
        ((_provider, model),) = question.choices
        asked = json.loads(question.prompt)
        said: dict[str, Any]
        if "part" in asked:
            heading = asked["part"]["heading"]
            self.asked.append((model, heading, "cover"))
            self.shown[heading] = asked
            told = self.said[heading]
            told = told[model] if isinstance(told, dict) else told
            said = {
                "answer": told[0],
                "records": [],
                "part_quote": "",
                "record_quote": "",
                "reason": "It reads so.",
            }
            if told[0] == "covered":
                chosen = next(rec for rec in asked["records"] if rec["title"] == told[1])
                said |= {
                    "records": [chosen["id"]],
                    "part_quote": told[2],
                    "record_quote": chosen["text"],
                }
        else:
            subject = asked["records"][0]["title"]
            if "concluded" in asked:
                self.asked.append((model, subject, "check"))
                said = {
                    "sentences": [
                        {"number": entry["number"], "verdict": "supported", "reason": ""}
                        for entry in asked["concluded"]
                    ]
                }
            else:
                self.asked.append((model, subject, "write"))
                self.shown["wrote:" + subject] = asked
                item = dict(self.writes[subject])
                if item["parent"]:
                    item["parent"] = next(
                        rec["id"] for rec in asked["records"] if rec["title"] == item["parent"]
                    )
                said = {
                    "answer": "written",
                    "reason": "The parts say what the product has to do.",
                    "parent": "",
                    "evidence": [],
                    "items": [item],
                }
        return Reply(
            said, "", "fixture", model, uuid4(), "Test", {"tokens": 60, "cost": 0.002}, 0.1, 1
        )


def shown(client: Any) -> dict[str, Any]:
    found = client.get(COVERAGE)
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


def lines(client: Any, listed: str) -> list[dict[str, Any]]:
    found = client.get(f"{COVERAGE}/lines/{listed}")
    assert found.status_code == 200, found.text
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def parts(client: Any, show: str = "all") -> dict[str, dict[str, Any]]:
    found = client.get(f"{COVERAGE}/parts", params={"show": show})
    assert found.status_code == 200, found.text
    return {item["heading"]: item for item in found.json()["items"]}


def story(repo: Any) -> UUID:
    with repo.sessions() as db:
        key: UUID = db.scalars(
            select(ProjectUserStory.id).where(ProjectUserStory.title == STORY)
        ).one()
        return key


def test_a_run_says_how_much_of_the_documentation_the_plan_covers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        documents(repo)
        assert post(client, "review-settings", **SAVE).status_code == 200
        before = shown(client)
        # Nothing was read into parts yet: the rules alone say what the parts are about.
        stands = before["stands"]
        assert (stands["parts"], stands["product"], stands["other"], stands["open"]) == (7, 1, 1, 5)
        assert stands["percent"] is None and not stands["read"] and before["can_start"]
        assert before["plan"]["answer"]["model"] == "analysis"
        assert before["plan"]["judge"]["model"] == "other"

        assert post(client, "coverage/runs").status_code == 200
        assert shown(client)["run"]["state"] == "running"
        model = Readers(SAID, WRITES)
        carried(repo, model, repo.coverage)
        # The part of the change named after issue 42, and how to contribute, were not
        # asked about: five parts, each of two models.
        covers = [entry for entry in model.asked if entry[2] == "cover"]
        assert len(covers) == 10 and {entry[0] for entry in covers} == {"analysis", "other"}
        assert {entry[1] for entry in covers} == set(SAID) - {"Calling the platform"}
        # A part is shown with the document it is in and the records nearest to it.
        asked = model.shown["Audit entries"]
        assert asked["part"]["document"] == "docs/audit.md" and ENTRIES in asked["part"]["text"]
        assert STORY in [rec["title"] for rec in asked["records"]]
        # Records were written for the two parts nothing covers, each from its part.
        wrote = model.shown["wrote:docs/audit.md: Exporting the audit log"]["records"]
        assert wrote[0]["id"] == "P1" and EXPORT in wrote[0]["text"]
        assert STORY in [rec["title"] for rec in wrote if rec["id"].startswith("S")]

        after = shown(client)
        ran = after["run"]
        assert ran["state"] == "complete" and all(stage["reached"] for stage in ran["stages"])
        assert ran["cost"] > 0
        stands = after["stands"]
        # Five parts are about the product: three are covered, one is in doubt, and one
        # waits for the Epic that was written for it.
        assert (stands["product"], stands["covered"], stands["doubt"], stands["none"]) == (
            5,
            3,
            1,
            1,
        )
        assert (stands["other"], stands["open"], stands["percent"]) == (2, 0, 60.0)
        listed = parts(client)
        assert listed["Why"]["covered_by"][0]["how"] == "stated"
        assert listed["Why"]["covered_by"][0]["ref"] == "#42"
        assert listed["Audit entries"]["covered_by"][0]["title"] == STORY
        assert listed["Audit entries"]["covered_by"][0]["how"] == "models"
        assert listed["How this folder is laid out"]["about"] == "other"
        assert listed["How this folder is laid out"]["by"] == "models"
        assert listed["Contributing"]["by"] == "rule"

        # What two models found covered is listed under Done, with what they quoted.
        done = lines(client, "done")
        assert [line["case"] for line in done] == ["covered"] and not done[0]["can_undo"]
        assert done[0]["candidates"][0]["title"] == STORY
        # An Issue was written for the export, under the User Story about the audit.
        created = lines(client, "created")
        assert [line["subject"]["title"] for line in created] == ["Export the audit entries"]
        made = created[0]
        assert made["made"]["kind"] == "issues" and made["can_undo"]
        assert made["action"]["for"]["title"] == STORY
        assert [source["kind"] for source in made["made"]["sources"]] == ["doc"]
        with repo.sessions() as db:
            issue = db.get_one(BacklogItem, UUID(made["made"]["id"]))
            # It is kept here: not on GitHub.
            assert issue.workspace_review == "pending" and issue.issue_number is None
            assert issue.description == (
                "Add an export of the audit entries of a period as a file.\n\n"
                "Done when:\n- An operator can download the file."
            )
        assert listed["Exporting the audit log"]["covered_by"][0]["how"] == "made"
        assert after["unpublished"] == 1

        needs = {line["case"]: line for line in lines(client, "needs_you")}
        assert set(needs) == {"differ", "draft_epic"} and after["waiting"] == {"drafts": 1}
        # Where the two models differ, the part waits with the records it was shown.
        doubt = needs["differ"]
        assert doubt["subject"]["title"] == "docs/audit.md: Retention"
        offered = {entry["title"]: entry for entry in doubt["candidates"]}
        assert offered[STORY]["chosen"] and not offered[STORY]["judge"]
        answered = post(
            client, f"coverage/lines/{doubt['id']}", choice="covered", record_id=str(story(repo))
        )
        assert answered.status_code == 200, answered.text
        assert parts(client)["Retention"]["covered_by"][0]["how"] == "you"
        # An Epic is never made without a word: it is made when the word is given.
        epic = needs["draft_epic"]
        assert epic["action"]["items"][0]["kind"] == "epics" and "at your word" in epic["reason"]
        assert post(client, "coverage/groups", group="drafts", choice="make").json() == {
            "answered": 1,
            "skipped": 0,
            "left": 0,
        }
        with repo.sessions() as db:
            assert db.scalars(select(ProjectEpic.title).where(ProjectEpic.title == "Quotas")).one()
        assert shown(client)["stands"]["percent"] == 100.0
        assert lines(client, "needs_you") == []

        # The Issue is taken away again: its part is covered by nothing, and it is not
        # written a second time.
        assert post(client, f"coverage/lines/{made['id']}/undo").status_code == 200
        with repo.sessions() as db:
            taken = db.get_one(BacklogItem, UUID(made["made"]["id"]))
            assert taken.workspace_review not in ("pending", "approved")
        now = shown(client)
        assert now["stands"]["percent"] == 80.0 and now["create"]["to_ask"] == 0
        assert not now["can_start"] and "nothing to check" in now["why_not"]

        # The administrator says a part is about the product after all.
        layout = parts(client, "other")["How this folder is laid out"]
        said = post(client, f"coverage/parts/{layout['id']}", about="product")
        assert said.status_code == 200 and said.json()["about"] == "product"
        now = shown(client)
        assert (now["stands"]["product"], now["stands"]["none"]) == (6, 2)
        assert now["create"]["to_ask"] == 1 and now["can_start"]
        assert parts(client, "none")["How this folder is laid out"]["by"] == "you"

        # The open records no part of the documentation speaks of.
        silent = client.get(COVERAGE + "/undocumented").json()
        titles = {item["title"] for item in silent["items"]}
        assert {"Respond to failures", "Unrelated work"} <= titles
        assert not {STORY, "Record error events", "Quotas"} & titles
    finally:
        client.close()


def test_connected_documentation_and_documents_that_change(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        pid = documents(repo)
        assert post(client, "review-settings", **SAVE).status_code == 200
        with repo.sessions.begin() as db:
            peer = db.scalars(
                select(ProjectRecord).where(ProjectRecord.name == "falcone-serverless")
            ).one()
            first, second = sorted((pid, peer.id), key=str)
            db.add(ProjectConnection(first_id=first, second_id=second, owner_id=pid))
            document(db, peer.id, "docs/platform.md", (("Calling the platform", CALLING),))

        assert post(client, "coverage/runs").status_code == 200
        model = Readers(SAID, WRITES)
        carried(repo, model, repo.coverage)
        # The part of the connected project's documentation that names this project was
        # checked against this project's plan, and is counted beside the number.
        asked = model.shown["Calling the platform"]
        assert asked["part"]["project"] == "falcone-serverless"
        after = shown(client)
        assert after["asks"] == {"parts": 1, "covered": 1, "none": 0, "doubt": 0}
        assert after["stands"]["parts"] == 7 and after["stands"]["percent"] == 60.0
        done = {line["subject"]["title"]: line for line in lines(client, "done")}
        assert done["docs/platform.md: Calling the platform"]["subject"]["ref"] == (
            "falcone-serverless"
        )
        created = lines(client, "created")
        assert [line["subject"]["title"] for line in created] == ["Export the audit entries"]
        assert not after["can_start"] and not after["plan"]["reread"]

        # The guide is read again: the part about the export is gone, and one is new.
        with repo.sessions.begin() as db:
            document(
                db,
                pid,
                "docs/audit.md",
                (AUDIT[0], AUDIT[2], AUDIT[3], ("Archiving", EXPORT[:60] + " " + LAYOUT)),
            )
            db.add(
                DocScan(
                    project_id=pid,
                    repository="gntik-ai/falcone",
                    branch="main",
                    state="complete",
                    reason="manual",
                    modified=1,
                    ended_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
        again = shown(client)
        assert again["plan"]["reread"] and again["can_start"]
        assert post(client, "coverage/runs").status_code == 200
        second_model = Readers({**SAID, "Archiving": ("other",)}, WRITES)
        carried(repo, second_model, repo.coverage)
        # Only the part that is new was asked about: the others keep their answers.
        assert {entry[1] for entry in second_model.asked if entry[2] == "cover"} == {"Archiving"}
        with repo.sessions() as db:
            gone = db.scalars(
                select(DocPart).where(DocPart.heading == "Exporting the audit log")
            ).one()
            assert gone.gone_at is not None
        assert "Exporting the audit log" not in parts(client)
        # The Issue that was made for the part that is gone is put before the
        # administrator, and taken away at their word.
        needs = {line["case"]: line for line in lines(client, "needs_you")}
        orphan = needs["source_gone"]
        assert orphan["subject"]["title"] == "Export the audit entries"
        taken = post(client, f"coverage/lines/{orphan['id']}", choice="take_away")
        assert taken.status_code == 200, taken.text
        with repo.sessions() as db:
            away = db.get_one(BacklogItem, UUID(created[0]["made"]["id"]))
            assert away.workspace_review not in ("pending", "approved")
        assert lines(client, "created") == [] and shown(client)["lists"]["created"] == 0
    finally:
        client.close()


def test_without_models_a_run_still_reads_what_the_documents_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        documents(repo)
        before = shown(client)
        assert before["plan"]["answer"] is None and before["can_start"]
        assert post(client, "coverage/runs").status_code == 200
        for _ in range(4):
            if repo.coverage.tick():
                break
        after = shown(client)
        assert after["run"]["state"] == "complete"
        assert "Not everything was asked" in after["run"]["detail"]
        stands = after["stands"]
        # The part of the change named after an Issue is covered; the rest is not checked.
        assert (stands["product"], stands["covered"], stands["open"], stands["other"]) == (
            1,
            1,
            5,
            1,
        )
        assert stands["percent"] == 100.0 and stands["read"]
        # Nothing can be asked until a model is named under Settings.
        assert not after["can_start"] and after["why_not"]
        # A project whose documentation was never read says so.
        other = client.get(BASE.replace("falcone", "llmwiki") + "coverage").json()
        assert other["stands"]["parts"] == 0 and not other["can_start"]
        assert "No documentation" in other["why_not"]
    finally:
        client.close()
