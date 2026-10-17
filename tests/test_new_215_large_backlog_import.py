"""NEW-215: large backlogs import (payload size, oldest-first selection, label scope)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer
from test_workflow_replay import _run_to_history

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.github import GitHubActivityHandler, _oldest_first_selection
from engineering_control.api.schemas import GitHubBacklogImportRequest
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogIssueRecord, GitHubBacklogCampaign
from engineering_control.engineering.backlog import (
    IMPORT_SNAPSHOT_ENCODING,
    ISSUE_LABEL_PATTERN,
    ISSUE_QUERY_BASE,
    approval_binding,
    decode_import_snapshot,
    encode_import_snapshot,
    issue_label_from_query,
    issue_query_for,
)
from engineering_control.workflows.definitions import (
    _BACKLOG_IMPORT_LABEL,
    GitHubBacklogImportWorkflow,
)
from engineering_control.workflows.inputs import GitHubBacklogImportInput

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "gntik-ai/gntik-ui"
HEAD_SHA = "a" * 40
CAMPAIGN_ID = "33333333-3333-4333-8333-333333333215"


def _body(number: int) -> str:
    # ~14 KB of realistic change-request Markdown: repeated structure, varied identifiers.
    lines = [f"kind: feature\nRisk: R1\n\n## Why\n\nIssue {number} needs a change."]
    while sum(len(line) for line in lines) < 14_000:
        digest = hashlib.sha256(f"{number}:{len(lines)}".encode()).hexdigest()
        word, line = digest[:8], int(digest[8:11], 16) % 400 + 1
        lines.append(
            f"- `{word}` in packages/ui/src/{word}/{word}.tsx:{line} renders "
            f"the {word} state without a label; add `aria-label` and a test for {word}."
        )
    return "\n".join(lines)


def _issue(number: int, body: str, labels: tuple[str, ...] = ("enhancement",)) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "title": f"Change request: component {number}",
        "body": body,
        "labels": [{"name": label} for label in labels],
        "assignees": [],
        "comments": [],
        "updatedAt": "2026-10-17T10:00:00Z",
    }


class _Broker:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.labels: list[str | None] = []
        self.listings: list[bool] = []

    def preflight(self, repositories: tuple[str, ...]) -> None:
        assert repositories == (REPOSITORY,)

    def repository_metadata(self, repository: str) -> dict[str, Any]:
        return {"defaultBranchRef": {"name": "main"}}

    def branch_commit(self, repository: str, branch: str) -> dict[str, str]:
        return {"sha": HEAD_SHA, "tree": "f" * 40}

    def list_open_issues(
        self, repository: str, label: str | None = None, *, oldest_first: bool = False
    ) -> list[dict[str, Any]]:
        # Like `gh issue list --limit 1000`: newest first unless sorted oldest first.
        self.labels.append(label)
        self.listings.append(oldest_first)
        scoped = [
            issue
            for issue in self.issues
            if label is None or label in {item["name"] for item in issue["labels"]}
        ]
        ordered = sorted(scoped, key=lambda issue: issue["number"], reverse=not oldest_first)
        return ordered[:1000]

    def list_repository_pull_requests(self, repository: str) -> list[dict[str, Any]]:
        return []


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'backlog.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=tmp_path,
        run_root=tmp_path / "runs",
    )


def _github(tmp_path: Path, broker: _Broker) -> GitHubActivityHandler:
    github = GitHubActivityHandler(_settings(tmp_path))
    github.broker = broker  # type: ignore[assignment]
    return github


def _newest_first(numbers: range) -> list[dict[str, Any]]:
    return [_issue(number, _body(number)) for number in sorted(numbers, reverse=True)]


async def test_a_500_issue_import_crosses_temporal_encoded_and_persists(tmp_path: Path) -> None:
    broker = _Broker(_newest_first(range(148, 694)))  # 546 open, newest first
    imported = await _github(tmp_path, broker).handle(
        "backlog.github_import",
        {"backlog_campaign_id": CAMPAIGN_ID, "project": "gntik-ui", "maximum_items": 500},
    )
    # The result that Temporal stores and hands to the next activity stays under 1 MB.
    assert len(json.dumps(imported).encode()) < 1_000_000
    assert "snapshot" not in imported
    assert imported["snapshot_encoding"] == IMPORT_SNAPSHOT_ENCODING
    assert imported["status"] == "IMPORTED_READ_ONLY" and imported["issue_count"] == 500
    # Oldest-first: the 500 lowest numbers, so 148..647 and never the oldest dropped.
    snapshot = decode_import_snapshot(imported)
    numbers = sorted(issue["number"] for issue in snapshot["repositories"][0]["issues"])
    assert numbers == list(range(148, 648))

    handler = EngineeringActivityHandler(_settings(tmp_path))
    Base.metadata.create_all(handler.engine)
    persisted = await handler.handle("backlog.persist_import", {**imported, "import_filter": "all"})
    assert persisted["status"] == "IMPORTED"
    # The snapshot is not carried into later steps.
    assert not {"snapshot", "snapshot_encoded", "snapshot_encoding"} & set(persisted)
    assert len(json.dumps(persisted, default=str).encode()) < 50_000
    planned = await handler.handle("backlog.plan", persisted)
    assert planned["status"] not in {"BLOCKED", "FAILED"}
    with handler.sessions() as session:
        campaign = session.get(GitHubBacklogCampaign, UUID(CAMPAIGN_ID))
        assert campaign is not None and campaign.issue_query == ISSUE_QUERY_BASE
        rows = session.scalars(select(BacklogIssueRecord)).all()
        assert sorted(row.issue_number for row in rows) == list(range(148, 648))


def test_oldest_first_selection_keeps_a_fitting_import_unchanged() -> None:
    listed = [{"number": number} for number in (40, 12, 33, 7)]
    assert _oldest_first_selection(listed, 10) == listed
    assert _oldest_first_selection(listed, 4) == listed
    assert _oldest_first_selection(listed, 2) == [{"number": 12}, {"number": 7}]
    assert _oldest_first_selection(listed, 0) == []


async def test_an_oversized_import_fails_with_counts_not_a_temporal_error(
    tmp_path: Path,
) -> None:
    # Incompressible bodies: even encoded, 500 of them exceed the guard.
    issues = [
        _issue(number, base64.b64encode(os.urandom(4500)).decode()) for number in range(500, 0, -1)
    ]
    with pytest.raises(ApplicationError) as raised:
        await _github(tmp_path, _Broker(issues)).handle(
            "backlog.github_import",
            {"backlog_campaign_id": CAMPAIGN_ID, "project": "gntik-ui", "maximum_items": 500},
        )
    assert raised.value.type == "BACKLOG_IMPORT_TOO_LARGE" and raised.value.non_retryable
    assert "500 issues" in str(raised.value)


def test_the_snapshot_codec_round_trips_and_reads_pre_new_215_results() -> None:
    snapshot = {"project": "gntik-ui", "repositories": [{"issues": [{"number": 1}]}]}
    encoded = {"snapshot_encoding": IMPORT_SNAPSHOT_ENCODING}
    encoded["snapshot_encoded"] = encode_import_snapshot(snapshot)
    assert decode_import_snapshot(encoded) == snapshot
    assert decode_import_snapshot({"snapshot": snapshot}) == snapshot
    truncated = {**encoded, "snapshot_encoded": encoded["snapshot_encoded"][:40]}
    with pytest.raises(ValueError):
        decode_import_snapshot(truncated)
    with pytest.raises(ValueError):
        decode_import_snapshot({**encoded, "snapshot_encoding": "gzip"})


async def test_a_label_scoped_import_is_recorded_and_bound_into_the_approval(
    tmp_path: Path,
) -> None:
    issues = [
        _issue(196, _body(196), ("enhancement", "batch:3")),
        _issue(300, _body(300), ("enhancement", "batch:7")),
        _issue(148, _body(148), ("enhancement", "batch:3")),
    ]
    broker = _Broker(issues)
    imported = await _github(tmp_path, broker).handle(
        "backlog.github_import",
        {
            "backlog_campaign_id": CAMPAIGN_ID,
            "project": "gntik-ui",
            "maximum_items": 500,
            "issue_label": "batch:3",
        },
    )
    assert broker.labels == ["batch:3"] and imported["issue_count"] == 2
    handler = EngineeringActivityHandler(_settings(tmp_path))
    Base.metadata.create_all(handler.engine)
    persisted = await handler.handle("backlog.persist_import", {**imported, "import_filter": "all"})
    await handler.handle("backlog.plan", persisted)
    candidate = await handler.handle(
        "backlog.approval_candidate",
        {
            "backlog_campaign_id": CAMPAIGN_ID,
            "project": "gntik-ui",
            "delivery_mode": "full-delivery",
            "maximum_risk": "R1",
            "maximum_items": 2,
        },
    )
    with handler.sessions() as session:
        campaign = session.get(GitHubBacklogCampaign, UUID(CAMPAIGN_ID))
        assert campaign is not None
        assert campaign.issue_query == f"{ISSUE_QUERY_BASE} label:batch:3"
        assert campaign.approval_json["issue_label"] == "batch:3"
        assert campaign.approval_binding == candidate["approval_binding"]
        rows = session.scalars(select(BacklogIssueRecord)).all()
        assert sorted(row.issue_number for row in rows) == [148, 196]


async def test_an_invalid_label_is_refused_before_github_is_read(tmp_path: Path) -> None:
    broker = _Broker([])
    for label in ("-x", "a b", "x" * 65, "batch;3"):
        with pytest.raises(ApplicationError) as raised:
            await _github(tmp_path, broker).handle(
                "backlog.github_import",
                {
                    "backlog_campaign_id": CAMPAIGN_ID,
                    "project": "gntik-ui",
                    "maximum_items": 5,
                    "issue_label": label,
                },
            )
        assert raised.value.type == "PolicyViolation"
    assert broker.labels == []


def test_an_unscoped_approval_binding_is_unchanged_and_a_label_changes_it() -> None:
    common: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "project": "gntik-ui",
        "import_filter": "all",
        "source_head": HEAD_SHA,
        "deployment_head": HEAD_SHA,
        "dependency_plan_hash": "d" * 64,
        "maximum_risk": "R1",
        "maximum_items": 2,
        "delivery_mode": "full-delivery",
        "include_enhancements": False,
        "now": datetime(2026, 10, 17, tzinfo=UTC),
    }
    unscoped = approval_binding(**common)
    assert "issue_label" not in unscoped.model_dump(mode="json")
    scoped = approval_binding(**common, issue_label="batch:3")
    assert scoped.model_dump(mode="json")["issue_label"] == "batch:3"
    assert scoped.sha256() != unscoped.sha256()


def test_the_label_query_round_trips_and_the_patterns_agree() -> None:
    assert issue_query_for(None) == ISSUE_QUERY_BASE
    assert issue_label_from_query(ISSUE_QUERY_BASE) is None
    assert issue_label_from_query("state:open") is None
    assert issue_label_from_query(issue_query_for("pkg:ui")) == "pkg:ui"
    assert _BACKLOG_IMPORT_LABEL.pattern == ISSUE_LABEL_PATTERN
    assert re.fullmatch(ISSUE_LABEL_PATTERN, "batch:3")
    with pytest.raises(ValidationError):
        GitHubBacklogImportRequest.model_validate(
            {"project": "gntik-ui", "import_filter": "all", "issue_label": "--repo"}
        )
    request = GitHubBacklogImportRequest.model_validate(
        {"project": "gntik-ui", "import_filter": "all", "issue_label": "batch:3"}
    )
    assert request.issue_label == "batch:3"


def test_the_cli_sends_the_label_only_when_given() -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload or {}}

    scoped = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(["backlog-import", "gntik-ui", "all", "--label", "batch:3"]),
    )
    unscoped = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(["backlog-import", "gntik-ui", "all"]),
    )
    assert scoped["payload"]["issue_label"] == "batch:3"  # type: ignore[index]
    assert "issue_label" not in unscoped["payload"]  # type: ignore[operator]


def _recording_import_steps(seen: list[dict[str, Any]]) -> Any:
    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        seen.append({"step": request["step"], "payload": request["payload"]})
        return {**request["payload"], "status": "PLANNED"}

    return execute_control_step


async def test_the_import_workflow_carries_the_label_and_replays() -> None:
    seen: list[dict[str, Any]] = []
    history, result = await _run_to_history(
        GitHubBacklogImportWorkflow,
        GitHubBacklogImportInput(
            backlog_campaign_id=CAMPAIGN_ID,
            project="gntik-ui",
            import_filter="all",
            maximum_items=50,
            issue_label="batch:3",
        ),
        _recording_import_steps(seen),
    )
    assert result is not None and result["issue_label"] == "batch:3"
    assert [item["step"] for item in seen] == [
        "backlog.github_import",
        "backlog.persist_import",
        "backlog.plan",
    ]
    assert seen[0]["payload"]["issue_label"] == "batch:3"
    await Replayer(workflows=[GitHubBacklogImportWorkflow]).replay_workflow(history)


async def test_an_unscoped_import_workflow_replays_and_a_bad_label_runs_nothing() -> None:
    seen: list[dict[str, Any]] = []
    history, result = await _run_to_history(
        GitHubBacklogImportWorkflow,
        GitHubBacklogImportInput(
            backlog_campaign_id=CAMPAIGN_ID, project="falcone", import_filter="all"
        ),
        _recording_import_steps(seen),
    )
    assert result is not None and result["issue_label"] is None
    await Replayer(workflows=[GitHubBacklogImportWorkflow]).replay_workflow(history)
    refused: list[dict[str, Any]] = []
    _, failed = await _run_to_history(
        GitHubBacklogImportWorkflow,
        GitHubBacklogImportInput(
            backlog_campaign_id=CAMPAIGN_ID,
            project="gntik-ui",
            import_filter="all",
            issue_label="--repo",
        ),
        _recording_import_steps(refused),
    )
    assert failed is None and refused == []


async def test_a_full_newest_first_list_is_relisted_oldest_first(tmp_path: Path) -> None:
    # 1,200 open: GitHub's newest-first list stops at 1,000 (#201-1200), so the activity
    # lists again oldest first and keeps #1-500, never #701-1200.
    broker = _Broker([_issue(number, f"Issue {number}.") for number in range(1200, 0, -1)])
    imported = await _github(tmp_path, broker).handle(
        "backlog.github_import",
        {"backlog_campaign_id": CAMPAIGN_ID, "project": "gntik-ui", "maximum_items": 500},
    )
    assert broker.listings == [False, True]
    snapshot = decode_import_snapshot(imported)
    numbers = sorted(issue["number"] for issue in snapshot["repositories"][0]["issues"])
    assert numbers == list(range(1, 501))


async def test_a_short_list_is_read_once_in_the_default_order(tmp_path: Path) -> None:
    broker = _Broker([_issue(number, f"Issue {number}.") for number in range(40, 0, -1)])
    imported = await _github(tmp_path, broker).handle(
        "backlog.github_import",
        {"backlog_campaign_id": CAMPAIGN_ID, "project": "gntik-ui", "maximum_items": 500},
    )
    assert broker.listings == [False]
    snapshot = decode_import_snapshot(imported)
    assert [issue["number"] for issue in snapshot["repositories"][0]["issues"]] == list(
        range(40, 0, -1)
    )


def test_a_stream_declaring_a_huge_dictionary_is_refused_within_the_memory_limit() -> None:
    import lzma
    import struct
    import zlib

    stream = bytearray(
        lzma.compress(
            b"{}", format=lzma.FORMAT_XZ, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 4096}]
        )
    )
    header_size = (stream[12] + 1) * 4
    header = stream[12 : 12 + header_size]
    position = header.index(bytes([0x21, 0x01])) + 2  # LZMA2 filter, 1 property byte
    header[position] = 40  # the largest dictionary-size code (4 GiB - 1)
    header[-4:] = struct.pack("<I", zlib.crc32(bytes(header[:-4])))
    stream[12 : 12 + header_size] = header
    payload = {
        "snapshot_encoding": IMPORT_SNAPSHOT_ENCODING,
        "snapshot_encoded": base64.b64encode(bytes(stream)).decode(),
    }
    with pytest.raises(ValueError):
        decode_import_snapshot(payload)
