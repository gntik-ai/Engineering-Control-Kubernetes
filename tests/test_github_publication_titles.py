from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.github.client import GhCliBroker, GhCliPolicyError


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self, executable: str, arguments: list[str], *, cwd: Path, **kwargs: Any
    ) -> CommandResult:
        assert executable == "gh" and cwd.is_dir()
        self.commands.append(arguments)
        if arguments[:2] == ["auth", "status"]:
            return CommandResult(0, "", "")
        if arguments[:2] == ["api", "user"]:
            return CommandResult(0, '{"login":"fixture","type":"User"}', "")
        if arguments[:2] == ["repo", "view"]:
            return CommandResult(0, '{"nameWithOwner":"gntik-ai/falcone-charts"}', "")
        if arguments[:2] == ["pr", "create"]:
            return CommandResult(0, "https://github.com/gntik-ai/falcone-charts/pull/1", "")
        raise AssertionError("unexpected typed operation")


@pytest.mark.parametrize(
    "title", ["changeset 12345678: api finding (deployment)", "bounded (source)", "a" * 240]
)
def test_generated_pair_title_reaches_one_typed_argument(tmp_path: Path, title: str) -> None:
    runner = RecordingRunner()
    broker = GhCliBroker(runner, tmp_path, writes_enabled=True, authenticated_home=tmp_path)  # type: ignore[arg-type]
    body = tmp_path / "body.md"
    body.write_text("Inert fixture body.")
    broker.create_pull_request(
        repository="gntik-ai/falcone-charts",
        head="agent/falcone/42/12345678-1234-1234-1234-123456789abc",
        base="main",
        title=title,
        body_file=body,
        change_binding="changeset:12345678-1234-1234-1234-123456789abc",
    )
    writes = [arguments for arguments in runner.commands if arguments[:2] == ["pr", "create"]]
    assert len(writes) == 1
    assert writes[0][writes[0].index("--title") + 1] == title
    assert writes[0][-1] == "--draft"


@pytest.mark.parametrize(
    "title",
    [
        "",
        "a" * 241,
        "--repo=off-list",
        "bad\x00title",
        "bad\ntitle",
        "bad\rtitle",
        "$(inert)",
        "${INERT}",
        "inert;value",
        "inert&&value",
        "inert||value",
        "`inert`",
    ],
)
def test_title_denies_invalid_or_metacharacter_inputs(title: str) -> None:
    with pytest.raises(GhCliPolicyError):
        GhCliBroker._pull_request_title(title)


def test_parentheses_are_not_enabled_in_branch_or_authority_values() -> None:
    with pytest.raises(GhCliPolicyError):
        GhCliBroker._value("branch(deployment)", "branch")
