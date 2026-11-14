"""Work that is not a GitHub issue, and the flows that produce it: an item of the
platform's own delivered like an issue, an audit of the code, a search for enhancements.
With a stand-in cluster and a stand-in GitHub."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.db.models import ProjectRecord, ProjectSection
from engineering_control.flow_library import FLOWS, run_for_item
from engineering_control.sandbox import audit, discover
from engineering_control.sandbox.launcher import Launcher
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BASE,
    BUNDLE,
    REPOSITORY,
    _claude,
    _implemented,
    _log,
    _ran,
    _reviewed,
    _verified,
    world,
)
from tests.test_sandbox_runs import SANDBOXES


@pytest.fixture
def site(world: Any) -> Any:  # noqa: F811 - the fixture of the delivery tests
    """The delivery tests' project, with the flows that produce items assigned and a
    launcher that knows the backlog."""
    world.backlog = BacklogItems(world.sessions)
    world.launcher = Launcher(
        world.runs, world.launcher.cluster, SANDBOXES, world.github, backlog=world.backlog
    )
    with world.sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        for key, flow in (("code-audit", "audit-code"), ("enhancement", "discover-enhancements")):
            section = session.query(ProjectSection).filter_by(project_id=project.id, key=key).one()
            section.flow_key = flow
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    return world


def _checked_out() -> str:
    return _log([_ran("checkout", f"{BASE}\n"), _ran("install 1")])


def _codex(answer: dict[str, Any]) -> str:
    return _log([_ran("agent", '{"type":"turn"}\n'), _ran("answer", json.dumps(answer))])


def _by_claude(answer: dict[str, Any]) -> str:
    return _log([_ran("agent", _claude(answer)), _ran("restore")])


def _prompt(site: Any) -> str:
    return str(site.job()["commands"][0]["stdin"])


# --- an item that is not an issue ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_item_of_the_platforms_own_is_delivered_without_an_issue(site: Any) -> None:
    written = site.backlog.add(
        "falcone-workflow",
        title="Export the report as CSV",
        section="issues",
        description="Add a button on the reports page.\nIt downloads what the table shows.",
    )
    assert written["description"].startswith("Add a button") and written["issue_number"] is None
    run = site.runs.start_item(UUID(written["id"]), toolchain=site.toolchain)
    assert run["kind"] == "deliver_issue" and run["title"] == "Export the report as CSV"
    assert run["data"]["branch"].startswith(f"engineering-control/item-{written['id'][:8]}-")
    # The run's row does not carry the item's whole text; the item has it.
    assert "body" not in run["data"]["issue"]

    await site.step("prepare", _log([_ran("checkout", f"{BASE}\n"), _ran("install 1")]))
    # Nothing was read from GitHub's issues: there is no issue.
    assert not [path for _method, path in site.hub.calls if "/issues/" in path]
    await site.launcher.tick()
    prompt = _prompt(site)
    assert f"You are the implementer for a backlog item of {REPOSITORY}." in prompt
    assert "It downloads what the table shows." in prompt and "issue #" not in prompt
    commit = next(c for c in site.job()["commands"] if c["name"] == "commit")
    assert commit["env"]["MESSAGE"] == "Export the report as CSV"
    site.fake.finish(_implemented())
    await site.launcher.tick()
    await site.launcher.tick()
    assert f"a change for a backlog item of {REPOSITORY}" in _prompt(site)
    site.fake.finish(_reviewed())
    await site.launcher.tick()
    await site.step("verify", _verified())
    await site.step("bundle", _log([_ran("bundle")], artifact=BUNDLE))
    site.runs.decide(UUID(run["id"]), "publish")
    await site.launcher.tick()
    ended = site.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "published")
    pull = site.hub.created["pull"]
    assert pull["title"] == "Export the report as CSV"
    assert "Resolves #" not in pull["body"] and "What was asked for" in pull["body"]
    assert "It downloads what the table shows." in pull["body"]
    assert site.hub.created["commit"]["message"] == "Export the report as CSV"
    # Nothing else would ever close the item: the pull request is what it asked for.
    item = site.backlog.get(UUID(written["id"]))
    assert item["state"] == "done" and item["facts"]["pull_request"]["number"] == 77
    await site.github.aclose()


def test_an_items_own_words_can_be_edited_and_an_issues_cannot(site: Any) -> None:
    written = site.backlog.add("falcone-workflow", title="Old words", section=None)
    edited = site.backlog.edit(
        UUID(written["id"]), title="  New   words ", description="Line\x00 one\n\tline two  "
    )
    assert edited["title"] == "New words" and edited["description"] == "Line one\n\tline two"
    assert site.backlog.edit(UUID(written["id"]), description="   ")["description"] is None
    with pytest.raises(ProjectInvalid, match="edit it on GitHub"):
        site.backlog.edit(site.item_id, title="Changed here")
    # A list shows the start of a text; the item has it whole.
    site.backlog.edit(UUID(written["id"]), description="x" * 5000)
    listed = site.backlog.listing(project="falcone-workflow", search="New words")["items"][0]
    assert len(listed["description"]) == 400
    assert len(site.backlog.get(UUID(written["id"]))["description"]) == 5000


def test_what_starting_an_item_runs_follows_its_sections_flow() -> None:
    assert run_for_item("deliver-issue", finding=False) == "deliver_issue"
    assert run_for_item("discover-enhancements", finding=False) == "deliver_issue"
    # In a section that audits, a finding is fixed and anything else asks for an audit.
    assert run_for_item("audit-code", finding=True) == "deliver_issue"
    assert run_for_item("audit-code", finding=False) == "audit_code"
    assert run_for_item("recover-deployment", finding=False) is None
    assert run_for_item(None, finding=False) is None
    assert FLOWS["audit-code"].as_dict()["section_action"] == "Start an audit"
    assert FLOWS["deliver-issue"].as_dict()["section_action"] is None
    assert FLOWS["change-request"].as_dict()["runs_items"] is False


# --- auditing code -----------------------------------------------------------------------


def _finding(title: str, **changes: Any) -> dict[str, Any]:
    return {
        "title": title,
        "lens": "security",
        "severity": "high",
        "confidence": 0.9,
        "expected_behavior": "A session that has expired is refused.",
        "actual_behavior": f"{title}.\nSeen in the session check.",
        "minimal_reproduction": ["Create a session.", "Set the clock forward.", "Call /me."],
        "affected_component": "session",
        "files": ["src/session.ts"],
        "security_impact": "An expired session keeps working.",
        "data_impact": None,
        "recommended_action": "Compare the expiry with the server's clock.",
        **changes,
    }


AUDITED = {
    "summary": "Three candidates in the session module.",
    "decision": "FAIL",
    "covered": ["src/session.ts", "src/auth/"],
    "not_covered": ["src/billing/"],
    "findings": [
        _finding("Expired sessions are accepted"),
        _finding("Tokens are logged", severity="medium", lens="observability"),
        _finding("Cookie lacks the Secure flag", severity="low"),
    ],
    "open_questions": ["Is the billing module in scope?"],
}


def _round(*entries: tuple[int, bool, bool, bool, bool, str | None]) -> dict[str, Any]:
    return {
        "summary": "Checked each candidate.",
        "findings": [
            {
                "number": number,
                "reproduced": reproduced,
                "positive_control": positive,
                "negative_control": negative,
                "environment_valid": valid,
                "duplicate_of": duplicate,
                "evidence": ["pnpm test session → 1 failed"],
                "note": f"Finding {number}: looked at it.",
            }
            for number, reproduced, positive, negative, valid, duplicate in entries
        ],
    }


@pytest.mark.asyncio
async def test_an_audit_proposes_only_the_findings_verified_twice(site: Any) -> None:
    with pytest.raises(ProjectInvalid, match="no flow that produces items"):
        site.runs.start_for_section("falcone-workflow", "issues", scope="", toolchain={})
    run = site.runs.start_for_section(
        "falcone-workflow",
        "code-audit",
        scope="  The session   module ",
        toolchain=site.toolchain,
    )
    assert (run["kind"], run["title"]) == ("audit_code", "Audit: The session module")
    assert run["item_id"] is None and run["data"]["scope"]["text"] == "The session module"
    with pytest.raises(ProjectConflict, match="already under way"):
        site.runs.start_for_section("falcone-workflow", "code-audit", scope="", toolchain={})

    await site.launcher.tick()
    prepare = site.job()
    # What the project needs is installed; its tests are not run beforehand.
    assert [c["name"] for c in prepare["commands"]] == ["checkout", "install 1"]
    assert prepare["commands"][0]["env"]["BRANCH"].startswith("engineering-control/audit-")
    site.fake.finish(_checked_out())
    await site.launcher.tick()

    # The auditor: ChatGPT first, as in the original, reading only.
    await site.launcher.tick()
    pod = site.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "audit"
    job = site.job()
    assert job["commands"][0]["argv"][:2] == ["codex", "exec"]
    assert job["commands"][-1]["name"] == "restore"
    assert json.loads(job["files"][0]["content"]) == audit.AUDIT_SCHEMA
    prompt = _prompt(site)
    assert f"You are the auditor of {REPOSITORY}" in prompt and BASE in prompt
    assert "<scope>\nThe session module\n</scope>" in prompt
    assert "Report FAIL only with minimal reproducible findings." in prompt
    assert "tenant-isolation" in prompt
    site.fake.finish(_codex(AUDITED))
    await site.launcher.tick()

    # Two rounds of verification, both by the other subscription, each on its own.
    for ordinal, answer in (
        (
            1,
            _round(
                (1, True, True, True, True, None),
                (2, False, True, True, True, None),
                (3, True, True, True, True, None),
            ),
        ),
        (
            2,
            _round(
                (1, True, True, True, True, None),
                (2, False, True, True, True, None),
                (3, False, True, True, True, None),
            ),
        ),
    ):
        await site.launcher.tick()
        pod = site.fake.only_pod()
        assert pod["metadata"]["labels"]["engineering-control/step"] == "verify"
        assert site.job()["commands"][0]["argv"][0] == "claude"
        prompt = _prompt(site)
        assert f"Independent reproduction ordinal: {ordinal}." in prompt
        assert "Finding 1: Expired sessions are accepted" in prompt
        assert "   2. Set the clock forward." in prompt
        # What the backlog already holds is shown, to recognise a duplicate.
        assert "- #11 Signing in fails with an expired cookie" in prompt
        assert "- pnpm run typecheck" in prompt
        assert "looked at it" not in prompt, "a round is not shown the one before"
        site.fake.finish(_by_claude(answer))
        await site.launcher.tick()

    await site.launcher.tick()
    ended = site.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "findings")
    assert ended["detail"] == (
        "3 candidate finding(s): 1 confirmed, 1 not reproduced, 1 reproduced once only."
    )
    result = ended["data"]["result"]
    assert [entry["disposition"] for entry in result["findings"]] == [
        "CONFIRMED",
        "REJECTED",
        "FLAKY",
    ]
    assert result["not_covered"] == ["src/billing/"]
    assert result["open_questions"] == ["Is the billing module in scope?"]

    found = [
        item
        for item in site.backlog.listing(project="falcone-workflow")["items"]
        if item["proposed_by"] == "audit"
    ]
    assert [item["title"] for item in found] == ["Expired sessions are accepted"]
    finding = site.backlog.get(UUID(found[0]["id"]))
    assert finding["id"] == result["findings"][0]["item_id"]
    assert finding["state"] == "proposed" and finding["section"]["key"] == "code-audit"
    assert finding["source_run_id"] == run["id"]
    assert finding["facts"]["kind"] == "finding" and finding["facts"]["severity"] == "high"
    assert "## Minimal reproduction\n1. Create a session." in finding["description"]
    assert "## Security impact\nAn expired session keeps working." in finding["description"]
    assert "## Data impact" not in finding["description"]
    assert "- Round 2: Finding 1: looked at it." in finding["description"]

    # A finding is something to fix: starting it delivers it.
    delivery = site.runs.start_item(UUID(finding["id"]), toolchain={})
    assert delivery["kind"] == "deliver_issue"
    assert delivery["data"]["issue"]["title"] == "Expired sessions are accepted"
    await site.github.aclose()


@pytest.mark.asyncio
async def test_an_item_of_a_section_that_audits_asks_for_an_audit(site: Any) -> None:
    asked = site.backlog.add(
        "falcone-workflow",
        title="Audit the payment module",
        section="code-audit",
        description="Look at refunds first.",
    )
    run = site.runs.start_item(UUID(asked["id"]), toolchain={})
    assert (run["kind"], run["title"]) == ("audit_code", "Audit: Audit the payment module")
    assert run["data"]["scope"] == {
        "title": "Audit the payment module",
        "text": "Look at refunds first.",
    }
    await site.step("prepare", _log([_ran("checkout", f"{BASE}\n")]))
    await site.launcher.tick()
    assert "<scope>\nAudit the payment module\n\nLook at refunds first.\n</scope>" in _prompt(site)
    nothing = {**AUDITED, "decision": "PASS", "findings": [], "summary": "Nothing found."}
    site.fake.finish(_codex(nothing))
    await site.launcher.tick()
    await site.launcher.tick()
    ended = site.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "no_findings")
    assert ended["data"]["result"]["covered"] == ["src/session.ts", "src/auth/"]
    # The request is done: the audit it asked for has been made.
    request = site.backlog.get(UUID(asked["id"]))
    assert request["state"] == "done" and request["facts"]["confirmed"] == 0
    await site.github.aclose()


@pytest.mark.asyncio
async def test_an_auditor_that_cannot_audit_or_does_not_answer_ends_the_run(site: Any) -> None:
    run = site.runs.start_for_section(
        "falcone-workflow", "code-audit", scope="The API", toolchain={}
    )
    await site.step("prepare", _log([_ran("checkout", f"{BASE}\n")]))
    await site.launcher.tick()
    assert "<scope>\nThe API\n</scope>" in _prompt(site)
    site.fake.finish(_log([_ran("agent", '{"type":"turn"}\n'), _ran("answer", "")]))
    await site.launcher.tick()
    # An audit without an answer is asked for once more, then given up.
    await site.step("audit", _log([_ran("agent", '{"type":"turn"}\n'), _ran("answer", "")]))
    ended = site.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "no_answer")
    assert "gave no answer twice" in ended["detail"]

    blocked = site.runs.start_for_section(
        "falcone-workflow", "code-audit", scope="The API", toolchain={}
    )
    await site.step("prepare", _log([_ran("checkout", f"{BASE}\n")]))
    answer = {**AUDITED, "decision": "BLOCKED", "findings": [], "summary": "The checkout is empty."}
    await site.step("audit", _codex(answer))
    ended = site.run(blocked["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "blocked")
    assert "The checkout is empty." in ended["detail"]
    await site.github.aclose()


@pytest.mark.asyncio
async def test_an_audit_without_a_scope_plans_areas_and_audits_each_in_turn(site: Any) -> None:
    run = site.runs.start_for_section("falcone-workflow", "code-audit", scope="", toolchain={})
    assert run["title"] == "Audit: the whole repository"
    await site.step("prepare", _checked_out())

    # First a plan: the areas of the repository, the riskiest first.
    await site.launcher.tick()
    pod = site.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "plan"
    assert json.loads(site.job()["files"][0]["content"]) == audit.PLAN_SCHEMA
    prompt = _prompt(site)
    assert f"You plan an audit of {REPOSITORY}" in prompt and "at most 6 areas" in prompt
    planned = {
        "summary": "Three areas; the fixtures were left out.",
        "areas": [
            {
                "name": "Sessions",
                "paths": ["src/session.ts", "src/auth/"],
                "why": "Sign-in.",
                "lenses": ["security", "made-up"],
            },
            {
                "name": "Billing",
                "paths": ["src/billing/"],
                "why": "Money.",
                "lenses": ["correctness"],
            },
            {
                "name": "Reports",
                "paths": ["src/reports/"],
                "why": "Reads data of others.",
                "lenses": [],
            },
            # An area without paths is no area.
            {"name": "Everything else", "paths": [], "why": "", "lenses": []},
        ],
        "left_out": ["test/fixtures/"],
    }
    site.fake.finish(_codex(planned))
    await site.launcher.tick()
    assert [area["name"] for area in site.run(run["id"])["data"]["areas"]] == [
        "Sessions",
        "Billing",
        "Reports",
    ]

    # Area 1: a candidate, verified twice.
    await site.launcher.tick()
    prompt = _prompt(site)
    assert "Scope: area 1 of 3 of a whole-repository audit, Sessions." in prompt
    assert "- src/auth/" in prompt and "Lenses to apply first: security." in prompt
    site.fake.finish(_codex({**AUDITED, "findings": AUDITED["findings"][:1]}))
    await site.launcher.tick()
    for ordinal in (1, 2):
        await site.launcher.tick()
        assert site.fake.only_pod()["metadata"]["labels"]["engineering-control/step"] == "verify"
        assert f"Independent reproduction ordinal: {ordinal}." in _prompt(site)
        assert "Finding 1: Expired sessions are accepted" in _prompt(site)
        site.fake.finish(_by_claude(_round((1, True, True, True, True, None))))
        await site.launcher.tick()

    # Area 2 cannot be audited: that is said, and the audit goes on.
    await site.launcher.tick()
    assert "Scope: area 2 of 3 of a whole-repository audit, Billing." in _prompt(site)
    site.fake.finish(
        _codex(
            {**AUDITED, "decision": "BLOCKED", "findings": [], "summary": "The package is empty."}
        )
    )
    await site.launcher.tick()

    # Area 3: its own candidate, numbered within the area, not reproduced.
    await site.launcher.tick()
    assert "Scope: area 3 of 3 of a whole-repository audit, Reports." in _prompt(site)
    site.fake.finish(
        _codex({**AUDITED, "findings": [_finding("Reports leak another tenant's rows")]})
    )
    await site.launcher.tick()
    for _ordinal in (1, 2):
        await site.launcher.tick()
        prompt = _prompt(site)
        assert "Finding 1: Reports leak another tenant's rows" in prompt
        assert "Expired sessions are accepted" not in prompt.split("<backlog>")[0]
        site.fake.finish(_by_claude(_round((1, False, True, True, True, None))))
        await site.launcher.tick()

    await site.launcher.tick()
    ended = site.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "findings")
    assert ended["detail"] == "2 candidate finding(s) in 3 area(s): 1 confirmed, 1 not reproduced."
    result = ended["data"]["result"]
    assert [(f["number"], f["area"], f["disposition"]) for f in result["findings"]] == [
        (1, "Sessions", "CONFIRMED"),
        (2, "Reports", "REJECTED"),
    ]
    assert [
        (a["name"], a["blocked"], a["candidates"], a["confirmed"]) for a in result["areas"]
    ] == [
        ("Sessions", False, 1, 1),
        ("Billing", True, 0, 0),
        ("Reports", False, 1, 0),
    ]
    assert result["summary"] == "Three areas; the fixtures were left out."
    assert "test/fixtures/" in result["not_covered"]
    found = [
        item
        for item in site.backlog.listing(project="falcone-workflow")["items"]
        if item["proposed_by"] == "audit"
    ]
    assert [item["title"] for item in found] == ["Expired sessions are accepted"]
    await site.github.aclose()


@pytest.mark.asyncio
async def test_a_plan_that_names_no_area_audits_the_repository_as_one_scope(site: Any) -> None:
    run = site.runs.start_for_section("falcone-workflow", "code-audit", scope="", toolchain={})
    await site.step("prepare", _checked_out())
    await site.step(
        "plan", _codex({"summary": "It is one small package.", "areas": [], "left_out": []})
    )
    await site.launcher.tick()
    assert "Scope: the whole repository." in _prompt(site)
    site.fake.finish(_codex({**AUDITED, "decision": "PASS", "findings": [], "summary": "Clean."}))
    await site.launcher.tick()
    await site.launcher.tick()
    ended = site.run(run["id"])
    assert (ended["outcome"], ended["detail"]) == ("no_findings", "The audit found nothing.")
    assert ended["data"]["result"]["areas"] == []
    assert audit.areas({"areas": [{"name": "", "paths": ["a/"]}] * 9})[0]["name"] == "a/"
    assert len(audit.areas({"areas": [{"name": "x", "paths": ["a/"]}] * 9})) == audit.MAX_AREAS
    await site.github.aclose()


def test_two_rounds_decide_a_finding_as_the_original_did() -> None:
    def tried(*rounds: tuple[bool, bool, bool, bool]) -> list[dict[str, Any]]:
        return [
            {
                "reproduced": r,
                "positive_control": p,
                "negative_control": n,
                "environment_valid": v,
                "duplicate_of": None,
            }
            for r, p, n, v in rounds
        ]

    yes = (True, True, True, True)
    assert audit.disposition(tried(yes, yes)) == "CONFIRMED"
    assert audit.disposition(tried((False, True, True, True), (False, True, True, True))) == (
        "REJECTED"
    )
    assert audit.disposition(tried(yes, (False, True, True, True))) == "FLAKY"
    # Reproduced, but without both controls every time: not confirmed.
    assert audit.disposition(tried(yes, (True, True, False, True))) == "FLAKY"
    # An environment that could not check it, or a round that said nothing: not verified.
    assert audit.disposition(tried(yes, (True, True, True, False))) == "BLOCKED"
    assert audit.disposition(tried(yes)) == "BLOCKED"
    known = tried(yes, yes)
    known[1]["duplicate_of"] = "#11 Signing in fails with an expired cookie"
    assert audit.disposition(known) == "DUPLICATE"


def test_an_auditors_answer_is_bounded_and_put_in_shape() -> None:
    many = [_finding(f"Finding {n}") for n in range(20)]
    odd = _finding("", lens="vibes", severity="catastrophic", confidence="high")
    found = audit.candidates({"findings": [odd, {"actual_behavior": ""}, "text", *many]})
    assert len(found) == audit.MAX_FINDINGS
    # The original's title: the first line of the actual behaviour.
    assert found[0]["title"] == "."
    assert (found[0]["lens"], found[0]["severity"], found[0]["confidence"]) == (
        "correctness",
        "low",
        0.0,
    )
    assert audit.candidates({}) == []
    assert set(audit.AUDIT_SCHEMA["properties"]["findings"]["items"]["required"]) == set(
        audit.AUDIT_SCHEMA["properties"]["findings"]["items"]["properties"]
    ), "the ChatGPT client wants every field of an answer required"


# --- looking for enhancements ------------------------------------------------------------


def _proposal(title: str, **changes: Any) -> dict[str, Any]:
    return {
        "title": title,
        "persona": "operator",
        "observed_problem": "Reports can only be read on the page.",
        "current_behavior": "The table cannot be taken elsewhere.",
        "proposed_outcome": f"{title}.",
        "user_value": "Reports can be shared.",
        "evidence": ["src/reports/table.tsx:10-80"],
        "success_metric": "Half of the report views end in an export.",
        "dependencies": [],
        "security_data_implications": "The export holds what the page shows.",
        "risk": "R1",
        "acceptance_criteria": ["A CSV file downloads.", "It has the table's columns."],
        "rollout_strategy": "Behind a setting, on for one project first.",
        "kill_switch": None,
        **changes,
    }


@pytest.mark.asyncio
async def test_a_search_proposes_only_what_the_review_approved(site: Any) -> None:
    run = site.runs.start_for_section(
        "falcone-workflow", "enhancement", scope="", toolchain=site.toolchain
    )
    assert (run["kind"], run["title"]) == (
        "discover_enhancements",
        "Enhancements: the whole repository",
    )
    await site.step("prepare", _checked_out())

    await site.launcher.tick()
    pod = site.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "discover"
    job = site.job()
    assert job["commands"][0]["argv"][:2] == ["codex", "exec"]
    assert json.loads(job["files"][0]["content"]) == discover.DISCOVER_SCHEMA
    prompt = _prompt(site)
    assert "Separate product friction from correctness defects" in prompt
    assert "Do not publish or implement anything" in prompt
    assert "- #11 Signing in fails with an expired cookie" in prompt
    found = {
        "summary": "Two ideas.",
        "enhancements": [
            _proposal("Export the report as CSV"),
            _proposal("Dark mode", risk="R0"),
            # Without evidence it is no proposal.
            _proposal("Make it better", evidence=[]),
        ],
    }
    site.fake.finish(_codex(found))
    await site.launcher.tick()

    await site.launcher.tick()
    pod = site.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "review"
    assert site.job()["commands"][0]["argv"][0] == "claude"
    prompt = _prompt(site)
    assert "Approve one only when it is not a defect or duplicate" in prompt
    assert "Proposal 2: Dark mode" in prompt and "Make it better" not in prompt
    reviewed = {
        "summary": "One is ready.",
        "reviews": [
            {
                "number": 1,
                "decision": "APPROVE",
                "summary": "Bounded and measurable.",
                "evidence": ["src/reports/table.tsx exists"],
                "duplicate_of": None,
            },
            {
                "number": 2,
                "decision": "REQUEST_CHANGES",
                "summary": "No acceptance criteria that can be measured.",
                "evidence": [],
                "duplicate_of": None,
            },
        ],
    }
    site.fake.finish(_by_claude(reviewed))
    await site.launcher.tick()
    await site.launcher.tick()

    ended = site.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "proposed")
    assert ended["detail"] == "2 proposal(s): 1 wait for your approval, 1 would need changes."
    listed = ended["data"]["result"]["proposals"]
    assert [entry["disposition"] for entry in listed] == ["APPROVE", "REQUEST_CHANGES"]
    made = site.backlog.get(UUID(listed[0]["item_id"]))
    assert (made["title"], made["state"], made["proposed_by"]) == (
        "Export the report as CSV",
        "proposed",
        "discovery",
    )
    assert made["section"]["key"] == "enhancement" and made["facts"]["risk"] == "R1"
    assert "## Acceptance criteria\n- [ ] A CSV file downloads." in made["description"]
    assert "## Kill switch\nNone." in made["description"]
    assert "approved it: Bounded and measurable." in made["description"]
    # Nothing was written to GitHub: the proposal waits in the backlog.
    assert not [call for call in site.hub.calls if call[0] == "POST" and "/issues" in call[1]]

    # The same search again proposes nothing twice.
    again = site.runs.start_for_section("falcone-workflow", "enhancement", scope="", toolchain={})
    await site.step("prepare", _checked_out())
    await site.launcher.tick()
    assert "- Export the report as CSV" in _prompt(site)
    site.fake.finish(_codex({"summary": "Same idea.", "enhancements": [found["enhancements"][0]]}))
    await site.launcher.tick()
    await site.step("review", _by_claude({"summary": "Fine.", "reviews": reviewed["reviews"][:1]}))
    await site.launcher.tick()
    repeated = site.run(again["id"])
    assert (repeated["outcome"], repeated["data"]["result"]["counts"]["DUPLICATE"]) == (
        "nothing_approved",
        1,
    )

    # An enhancement is delivered like any other item once it is started.
    delivery = site.runs.start_item(UUID(made["id"]), toolchain={})
    assert delivery["kind"] == "deliver_issue"
    assert site.backlog.get(UUID(made["id"]))["state"] == "accepted"
    await site.github.aclose()


def test_a_review_lets_a_proposal_through_only_by_approving_it() -> None:
    assert discover.verdict(None) == "NOT_REVIEWED"
    assert discover.verdict({"decision": "APPROVE", "duplicate_of": None}) == "APPROVE"
    assert discover.verdict({"decision": "APPROVE", "duplicate_of": "#4 Export"}) == "DUPLICATE"
    assert discover.verdict({"decision": "REQUEST_CHANGES"}) == "REQUEST_CHANGES"
    assert discover.verdict({"decision": "LGTM"}) == "BLOCKED"
    assert len(discover.proposals({"enhancements": [_proposal(str(n)) for n in range(9)]})) == (
        discover.MAX_PROPOSALS
    )
    odd = discover.proposals({"enhancements": [_proposal("", risk="R9", persona="")]})[0]
    assert (odd["title"], odd["risk"], odd["persona"]) == (".", "R2", "user")


# --- the console -------------------------------------------------------------------------


def test_the_console_starts_a_sections_run_and_edits_an_item(site: Any) -> None:
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(str(site.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    with client:
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        sections = client.get("/api/dashboard/projects/falcone-workflow").json()["sections"]
        by_key = {section["key"]: section for section in sections}
        assert by_key["code-audit"]["flow"]["section_action"] == "Start an audit"
        assert by_key["enhancement"]["flow"]["section_action"] == "Look for enhancements"
        assert by_key["issues"]["flow"]["section_action"] is None

        started = post("/projects/falcone-workflow/sections/code-audit/run", scope="The API")
        assert started.status_code == 201, started.text
        assert started.json()["kind"] == "audit_code"
        assert post("/projects/falcone-workflow/sections/code-audit/run").status_code == 409
        assert post("/projects/falcone-workflow/sections/issues/run").status_code == 422
        assert post("/projects/falcone-workflow/sections/nowhere/run").status_code == 404
        assert "Run Audit: The API" in client.get("/api/dashboard/activity/recent").text

        added = post(
            "/projects/falcone-workflow/items",
            title="Export as CSV",
            section="enhancement",
            description="From the reports page.",
        )
        assert added.status_code == 201 and added.json()["description"] == "From the reports page."
        item_id = added.json()["id"]
        edited = post(f"/items/{item_id}/edit", description="From every table.")
        assert edited.status_code == 200 and edited.json()["title"] == "Export as CSV"
        one = client.get(f"/api/dashboard/items/{item_id}").json()
        assert one["description"] == "From every table." and one["run"] is None
        assert client.get(f"/api/dashboard/items/{UUID(int=0)}").status_code == 404
        # An issue of the repository is edited on GitHub.
        assert post(f"/items/{site.item_id}/edit", title="Changed").status_code == 422
        # Starting an enhancement delivers it.
        begun = post(f"/items/{item_id}/deliver")
        assert begun.status_code == 201 and begun.json()["kind"] == "deliver_issue"
