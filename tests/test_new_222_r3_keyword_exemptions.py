"""NEW-222: per-project R3 keyword exemptions (gntik-ui's UI vocabulary).

In gntik-ui "destructive" names the destructive button and tone variant and "production"
a production build, so the operator exempted both roots there (2026-10-17). Only the
keyword trigger is exempted: the declared ``Risk:`` floor, the security label, security
defects, severity, the operator R1 label and the other roots are unchanged, and every
other project keeps every root. The exemption is keyed by the project that owns the
issue's repository, so the import and the amend escalation check share one source.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engineering_control.engineering import backlog
from engineering_control.engineering.backlog import (
    PROJECT_R3_KEYWORD_EXEMPTIONS,
    R3_KEYWORD_ROOTS,
    GitHubIssueSnapshot,
    calculated_issue_risk,
    classify_issue,
    repository_r3_keyword_exemptions,
)
from engineering_control.projects import PROJECT_REPOSITORIES

NOW = datetime(2026, 10, 17, tzinfo=UTC)
GNTIK = "gntik-ai/gntik-ui"
TITLE = "Change request: Button: confirm destructive actions"


def _issue(
    body: str,
    *,
    repository: str = GNTIK,
    title: str = TITLE,
    labels: tuple[str, ...] = ("enhancement", "library-review"),
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        repository=repository,
        number=149,
        url=f"https://github.com/{repository}/issues/149",
        title=title,
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=labels,
        assignees=(),
        comments=(),
        updated_at=NOW,
    )


def _change_request(risk: str, extra: str = "") -> str:
    return (
        f"kind: feature\nRisk: {risk}\n\n## Why\n- The `destructive` tone of Button has no "
        "confirm step, and the production build tree-shakes the dialog.\n" + extra
    )


def _project(repository: str) -> str:
    return next(name for name, pair in PROJECT_REPOSITORIES.items() if repository in pair)


def _risk(snapshot: GitHubIssueSnapshot) -> str:
    return classify_issue(
        snapshot, (), project=_project(snapshot.repository), import_filter="all"
    ).calculated_risk


def test_only_gntik_ui_exempts_and_only_destructive_and_production() -> None:
    assert dict(PROJECT_R3_KEYWORD_EXEMPTIONS) == {
        "gntik-ui": frozenset({"destructive", "production"})
    }
    assert repository_r3_keyword_exemptions(GNTIK) == {"destructive", "production"}
    for project, pair in PROJECT_REPOSITORIES.items():
        for repository in pair:
            expected = PROJECT_R3_KEYWORD_EXEMPTIONS.get(project, frozenset())
            assert repository_r3_keyword_exemptions(repository) == expected
    assert repository_r3_keyword_exemptions("gntik-ai/falcone-charts") == frozenset()
    assert repository_r3_keyword_exemptions("evil/gntik-ui") == frozenset()
    assert repository_r3_keyword_exemptions("gntik-ai/unknown") == frozenset()


@pytest.mark.parametrize(("declared", "expected"), [("R1", "R1"), ("R2", "R2"), ("R3", "R3")])
def test_an_exempt_keyword_leaves_the_declared_risk_floor(declared: str, expected: str) -> None:
    assert _risk(_issue(_change_request(declared))) == expected


@pytest.mark.parametrize("repository", ["gntik-ai/falcone", "gntik-ai/falcone-charts"])
def test_falcone_keeps_every_root(repository: str) -> None:
    assert _risk(_issue(_change_request("R1"), repository=repository)) == "R3"


@pytest.mark.parametrize(
    "extra",
    [
        "Run a data migration of the stored presets.\n",
        "Document the credential rotation of the registry token.\n",
        "Describe the secret lifecycle of the MCP key.\n",
        "Grant a ClusterRole to the docs job.\n",
    ],
)
def test_other_roots_still_raise_a_gntik_ui_issue_to_r3(extra: str) -> None:
    assert _risk(_issue(_change_request("R1", extra))) == "R3"


def test_security_label_still_raises_a_gntik_ui_issue_to_r3() -> None:
    snapshot = _issue(
        "The `destructive` Button leaks the session token into the DOM.\n",
        title="bug: destructive Button leaks the session token",
        labels=("bug", "security"),
    )
    assert _risk(snapshot) == "R3"


def test_severity_still_raises_a_gntik_ui_issue_to_r2() -> None:
    snapshot = _issue(
        "The destructive tone is unreadable in the production build.\n",
        title="bug: destructive tone contrast",
        labels=("bug", "severity:high"),
    )
    assert _risk(snapshot) == "R2"


def test_the_operator_r1_label_still_wins() -> None:
    snapshot = _issue(_change_request("R3"), labels=("enhancement", "hermes:risk-r1"))
    assert _risk(snapshot) == "R1"


def test_inflections_of_an_exempt_root_are_exempt_too() -> None:
    body = "kind: feature\nRisk: R1\n\nDestructively styled, **destructive** and productionised.\n"
    assert _risk(_issue(body)) == "R1"
    assert _risk(_issue(body, repository="gntik-ai/falcone")) == "R3"


@pytest.mark.parametrize("repository", [GNTIK, "gntik-ai/falcone"])
def test_the_amend_classification_matches_the_import(repository: str) -> None:
    for body in (_change_request("R1"), _change_request("R2"), "plain text, no keyword\n"):
        imported = _risk(_issue(body, repository=repository))
        amended = calculated_issue_risk(repository, ["enhancement", "library-review"], TITLE, body)
        assert imported == amended


def test_root_names_cover_exactly_the_old_roots() -> None:
    assert tuple(R3_KEYWORD_ROOTS.values()) == (
        r"data\s+migrat",
        r"destructiv",
        r"credential\s+rotat",
        r"secret\s+lifecycle",
        r"production",
        r"clusterrole",
    )
    assert backlog._r3_keywords(frozenset()) is backlog._R3_KEYWORDS


def test_without_exemptions_the_module_matcher_is_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backlog._r3_keywords(frozenset())  # warm any cache
    replacement = re.compile("zzz-only")
    monkeypatch.setattr(backlog, "_R3_KEYWORDS", replacement)
    assert backlog._r3_keywords(frozenset()) is replacement
    assert backlog._risk(backlog.BacklogIssueType.PRODUCT_BUG, "low", "destructive") == "R1"
    assert backlog._risk(backlog.BacklogIssueType.PRODUCT_BUG, "low", "zzz-only") == "R3"


def test_exempting_every_root_leaves_the_other_r3_triggers() -> None:
    every = frozenset(R3_KEYWORD_ROOTS)
    assert backlog._r3_keywords(every) is None
    assert backlog._risk(backlog.BacklogIssueType.SECURITY_DEFECT, "low", "x", every) == "R3"
    assert backlog._risk(backlog.BacklogIssueType.PRODUCT_BUG, "high", "x", every) == "R2"


@pytest.mark.asyncio
async def test_the_amend_escalation_check_keeps_falcone_strict(tmp_path: Path) -> None:
    """The amend path classifies through the same function, so a falcone amendment that
    adds "destructive" still escalates (NEW-115 behaviour is unchanged)."""
    from test_issue_delivery_approval import _dispose
    from test_new_115_issue_amend import APPROVED, _amend, _approved, _refresh

    from engineering_control.operations.issue_amend import IssueAmendBlocked

    values = await _approved(tmp_path)
    operations = values[0]
    edited = APPROVED + "\n\n## Addendum\nUse the destructive tone for the delete button."
    with pytest.raises(IssueAmendBlocked) as raised:
        await _amend(operations, await _refresh(tmp_path, edited), None)
    assert raised.value.blocker == "ISSUE_AMEND_RISK_ESCALATION"
    _dispose(values)
