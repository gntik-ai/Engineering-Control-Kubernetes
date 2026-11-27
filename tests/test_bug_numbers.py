"""Bugs seen and told (B4): the numbers of a project's bugs match the list they lead to;
the table of all projects and the digest say what is open; a critical bug that arrives is
told in the chat, once; and a bug is reported from the chat after the administrator has
seen what was understood."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select

from engineering_control.dashboard import bug_numbers
from engineering_control.db.models import (
    BacklogItem,
    BugRecord,
    ProjectRecord,
    WorkspaceAction,
)
from tests.test_bug_section import GitHub, listed, post, synchronize
from tests.test_chat import ADMINISTRATOR, _message, world  # noqa: F401 - ``world`` is a fixture
from tests.test_dashboard_web import _authenticated_client

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
PROJECT = "falcone-workflow"


def _reported_on(repo: Any, days: dict[int, int]) -> None:
    """Say when each bug was opened on GitHub: so many days before NOW."""
    with repo.sessions.begin() as db:
        for number, ago in days.items():
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == number)).one()
            item.facts = {
                **(item.facts or {}),
                "github_created_at": (NOW - timedelta(days=ago)).isoformat(),
            }


def test_the_numbers_of_the_bugs_match_the_list(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        for title, severity in (
            ("Rounding loses half a cent", "critical"),
            ("The export is empty", "high"),
            ("A tooltip is cut off", ""),
            ("Signing in fails", "high"),
            ("The list flickers", "low"),
        ):
            body = {
                "title": title,
                "happens": "It is wrong.",
                **({"severity": severity} if severity else {}),
            }
            assert post(client, "llmwiki", "bugs", **body).status_code == 200
        rows = {bug["number"]: bug for bug in listed(client, "llmwiki")["items"]}
        # Areas, and what caused one of them: that makes it a regression.
        for number, change in (
            (100, {"area": "billing", "caused_by": "#412"}),
            (101, {"area": "billing"}),
            (103, {"area": "console"}),
        ):
            done = post(client, "llmwiki", f"bugs/{rows[number]['id']}/triage", **change)
            assert done.status_code == 200, done.text
        # One is fixed; one was closed and came back.
        assert (
            post(client, "llmwiki", f"bugs/{rows[104]['id']}/close", reason="fixed").status_code
            == 200
        )
        assert (
            post(client, "llmwiki", f"bugs/{rows[103]['id']}/close", reason="fixed").status_code
            == 200
        )
        synchronize(repo, github)
        assert post(client, "llmwiki", f"bugs/{rows[103]['id']}/reopen").status_code == 200
        _reported_on(repo, {100: 40, 101: 10, 102: 2, 103: 6, 104: 12})
        with repo.sessions.begin() as db:
            fixed = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 104)).one()
            fixed.github_closed_at = NOW - timedelta(days=4)
            # A label on the issue says so too.
            labelled = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 102)).one()
            labelled.issue_labels = [*labelled.issue_labels, "Regression"]

        numbers = bug_numbers.report(repo.sessions, "llmwiki", now=NOW)

        def total(**narrowed: Any) -> int:
            return int(listed(client, "llmwiki", **narrowed)["total"])

        # Every number is the size of the list it leads to.
        assert numbers["open"] == total(state="open") == 4
        for severity, count in numbers["severities"].items():
            assert count == total(state="open", severity=severity), severity
        assert numbers["severities"] == {"critical": 1, "high": 2, "medium": 0, "low": 0, "none": 1}
        assert (numbers["new"], numbers["confirmed"]) == (
            total(state="new"),
            total(state="confirmed"),
        )
        assert [(entry["area"], entry["open"]) for entry in numbers["areas"]] == [
            ("billing", 2),
            ("console", 1),
            (None, 1),
        ]
        for entry in numbers["areas"]:
            assert entry["open"] == total(state="open", area=entry["area"] or "none")
        assert numbers["areas"][0]["severities"]["critical"] == 1
        assert numbers["regressions"] == {
            "open": total(state="open", mark="regression"),
            "all": total(state="", mark="regression"),
        }
        assert numbers["regressions"] == {"open": 2, "all": 2}
        assert numbers["reopened"] == {
            "open": total(state="open", mark="reopened"),
            "all": total(state="", mark="reopened"),
        }
        assert numbers["reopened"] == {"open": 1, "all": 1}
        (back,) = listed(client, "llmwiki", mark="reopened")["items"]
        assert (back["number"], back["reopened"], back["regression"]) == (103, 1, False)
        (caused,) = [b for b in listed(client, "llmwiki")["items"] if b["caused_by"]]
        assert (caused["number"], caused["caused_by"], caused["regression"]) == (100, "#412", True)

        # How old the open ones are; what arrived and was fixed lately; how long a fix took.
        assert numbers["age"] == {"oldest": 40.0, "median": 8.0}
        assert (numbers["days"], numbers["arrived"], numbers["fixed"]) == (30, 4, 1)
        assert numbers["fix_time"] == {"days": 90, "bugs": 1, "median": 8.0, "longest": 8.0}

        # The page asks for the same, and the table of all projects says it in short.
        served = client.get("/api/dashboard/projects/llmwiki/bugs/numbers")
        assert served.status_code == 200, served.text
        assert served.json()["severities"] == numbers["severities"]
        assert client.get("/api/dashboard/projects/unknown/bugs/numbers").status_code == 404
        table = {
            row["project"]: row["bugs"]
            for row in client.get("/api/dashboard/rollout").json()["projects"]
        }
        assert table["llmwiki"] == {
            "open": 4,
            "severities": numbers["severities"],
            "new": 1,
            "confirmed": 3,
            "in_progress": 0,
        }
        assert table["falcone"]["open"] == 0
        assert bug_numbers.said(table["llmwiki"]) == (
            "4 open: 1 critical, 2 high, 1 with no severity yet"
        )
        assert bug_numbers.said(table["falcone"]) == "no open bug"

        # The digest: what arrived and was fixed since yesterday, and what is open.
        _reported_on(repo, {101: 0})
        with repo.sessions.begin() as db:
            fixed = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 104)).one()
            fixed.github_closed_at = NOW - timedelta(hours=3)
        assert bug_numbers.digest_lines(repo.sessions, NOW - timedelta(days=1)) == [
            "Bugs:",
            "- **llmwiki**: 1 arrived, 1 fixed; 4 open: 1 critical, 2 high, 1 with no "
            "severity yet.",
        ]
    finally:
        client.close()


def _bug(db: Any, number: int, title: str, *labels: str) -> BacklogItem:
    project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == PROJECT)).one()
    item = BacklogItem(
        project_id=project.id,
        title=title,
        source="github",
        state="proposed",
        repository="gntik-ai/falcone-workflow",
        issue_number=number,
        issue_url=f"https://github.com/gntik-ai/falcone-workflow/issues/{number}",
        issue_state="open",
        issue_labels=["bug", *labels],
    )
    db.add(item)
    db.flush()
    return item


def _id(world: Any, number: int) -> Any:  # noqa: F811
    with world.sessions() as db:
        return db.scalars(select(BacklogItem.id).where(BacklogItem.issue_number == number)).one()


async def test_a_critical_bug_that_arrives_is_told_in_the_chat_once(world: Any) -> None:  # noqa: F811
    with world.sessions.begin() as db:
        _bug(db, 31, "Tenants read each other's files", "severity: critical")
        _bug(db, 32, "A label is misspelt", "severity: low")
        # One the administrator called critical themselves is no news to them.
        mine = _bug(db, 33, "Payments are charged twice")
        db.add(
            BugRecord(
                project_id=mine.project_id,
                item_id=mine.id,
                severity="critical",
                confirmed_at=datetime.now(UTC),
            )
        )
    assert await world.channel.deliver() == 1
    (told,) = world.transport.texts()
    assert told.startswith(
        "A critical bug in Falcone workflow: **#31 Tenants read each other's files**."
    )
    assert "https://github.com/gntik-ai/falcone-workflow/issues/31" in told
    assert told.endswith("https://ade.musematic.ai/console/workspace/falcone-workflow/bugs")
    # Not looked for again within the minute; and told once, however often it is.
    assert await world.channel.deliver() == 0
    world.channel._bugs_looked = None
    assert await world.channel.deliver() == 0
    told = frozenset({str(_id(world, 31))})
    assert bug_numbers.critical(world.sessions, world.channel.since(), told) == []
    assert [
        bug["number"] for bug in bug_numbers.critical(world.sessions, world.channel.since())
    ] == [31]
    # One from before the moment the channel counts from is no news.
    assert bug_numbers.critical(world.sessions, datetime.now(UTC) + timedelta(minutes=1)) == []

    # /bugs says what is open in every project; the digest says what arrived.
    await world.channel.handle(_message("/bugs"))
    (said,) = world.transport.texts()
    assert said == (
        "**Open bugs**\n- **falcone-workflow**: 3 open: 2 critical, 1 low, 2 new.\n"
        "To report one, write: Report a bug in <project>: what happens."
    )
    digest = world.channel.digest(datetime.now(UTC) + timedelta(minutes=1))
    assert "Bugs:\n- **falcone-workflow**: 3 arrived; 3 open: 2 critical, 1 low." in digest


async def test_a_bug_is_reported_from_the_chat_after_what_was_understood_is_shown(
    world: Any,  # noqa: F811
) -> None:
    with world.sessions.begin() as db:
        _bug(db, 31, "The export leaves out the last row of a page")
    run_id = world.turn()
    definition = world.toolbox.conversation.definitions.current()
    assert definition["tools"]["report_bug"] == {"enabled": True, "mode": "confirm"}

    async def report(**said: Any) -> str:
        return cast(
            str, (await world.toolbox.call(run_id, definition, "report_bug", dict(said))).text
        )

    bug = {
        "project": PROJECT,
        "title": "The export leaves out the last row",
        "happens": "The CSV has one row less than the page shows.",
        "steps": "Open the report.\nPress Export.",
        "severity": "high",
        "reason": "The administrator asked to report it.",
    }
    # Nothing is created at once, whatever the supervisor's settings say.
    assert (await report(**bug)).startswith("Not done: this waits for the administrator")
    with world.sessions() as db:
        assert not db.scalars(select(WorkspaceAction)).all()
    # What it does not know it cannot report, and it is told why.
    assert "Say what happens." in await report(**{**bug, "happens": " "})
    assert "A severity is" in await report(**{**bug, "severity": "urgent"})
    assert "Nothing with that name" in await report(**{**bug, "project": "nowhere"})

    # The administrator sees what was understood, and the open bugs that look alike.
    assert await world.channel.deliver() == 1
    ((text, buttons),) = world.transport.sent
    world.transport.sent.clear()
    assert text.startswith(
        "The supervisor proposes: **Report a bug in falcone-workflow, severity high: "
        "The export leaves out the last row**"
    )
    assert "Accepting creates the issue on GitHub, with the bug label." in text
    assert "What happens: The CSV has one row less than the page shows." in text
    assert "How to reproduce: Open the report. Press Export." in text
    assert "Open bugs that look alike: #31 The export leaves out the last row of a page" in text
    assert buttons is not None and buttons[0][0] == "Accept"

    # Accepted: the bug is kept with its severity, and its issue is queued for GitHub.
    press = {
        "update_id": 9,
        "callback_query": {
            "id": "cb1",
            "from": {"id": ADMINISTRATOR},
            "data": buttons[0][1],
            "message": {"message_id": 77},
        },
    }
    await world.channel.handle(press)
    assert world.transport.answered == ["Accepted."]
    with world.sessions() as db:
        made = db.scalars(
            select(BacklogItem).where(BacklogItem.title == "The export leaves out the last row")
        ).one()
        assert (made.execution_type, made.type_confirmed) == ("bug", True)
        assert made.workspace_review == "publishing" and made.issue_number is None
        assert "## What happens" in (made.description or "")
        assert "## How to reproduce" in (made.description or "")
        record = db.scalars(select(BugRecord).where(BugRecord.item_id == made.id)).one()
        assert record.severity == "high" and record.confirmed_at is not None
        (queued,) = db.scalars(select(WorkspaceAction)).all()
        assert (queued.operation, queued.local_id) == ("create", made.id)
    # The same bug is not reported twice.
    assert "A bug with this title is open already" in await report(**bug)

    # Rejected: nothing is created.
    other = {**bug, "title": "The import duplicates a row"}
    del other["severity"]
    assert (await report(**other)).startswith("Not done:")
    world.transport.sent.clear()
    await world.channel.deliver()
    text, buttons = next(entry for entry in world.transport.sent if entry[1])
    assert "Report a bug in falcone-workflow: The import duplicates a row" in text
    press["callback_query"]["data"] = buttons[1][1]
    await world.channel.handle(press)
    assert world.transport.answered[-1] == "Rejected."
    with world.sessions() as db:
        assert len(db.scalars(select(WorkspaceAction)).all()) == 1
