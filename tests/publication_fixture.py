"""Actual approved metadata and ASGI authority; only GitHub commands are recorded.

This fixture requires the suite's external-egress isolation. It never loads a
host credential: the worker token and all mutable paths belong to tmp_path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_issue_delivery_approval import _child, _dispose

from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.adapters.publication_authority import PublicationAuthorityClient
from engineering_control.config import Settings
from engineering_control.db.models import BacklogIssueRecord, Step
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.evidence.ingest import create_evidence_ingest_app
from engineering_control.operations.publication_authority import (
    EFFECT_STEP,
    PublicationAuthorityRepository,
)

BODY = "Inert approved publication fixture."


@dataclass
class ApprovedPublication:
    root: Path
    metadata: MetadataActivityHandler
    repository: SQLMetadataRepository
    branch: str
    heads: dict[str, str]
    issue_snapshot: dict[str, Any]
    input: dict[str, Any]
    prepared: dict[str, Any]

    @contextmanager
    def worker(self, runner: Any, settings: Settings) -> Iterator[DeliveryActivityHandler]:
        token_path = self.root / "publication-fixture.token"
        # Synthetic token is valid only in this in-process fixture application.
        token = "publication-fixture-only-" + uuid4().hex
        token_path.write_text(token)
        token_path.chmod(0o600)
        settings = Settings(
            _env_file=None,
            **{
                **settings.model_dump(),
                "database_url": str(self.metadata.engine.url),
                "worker_kind": "delivery",
                "evidence_ingest_url": "http://127.0.0.1:8750/v1/evidence",
                "evidence_ingest_token_file": token_path,
            },
        )
        authority = PublicationAuthorityRepository(str(self.metadata.engine.url))
        try:
            app = create_evidence_ingest_app(
                settings=settings,
                repository=self.repository,
                publication_authority=authority,
                tokens={"delivery-runner": token},
            )
            with TestClient(app, base_url="http://127.0.0.1:8750") as http:
                handler = DeliveryActivityHandler(settings, runner)
                handler.publication_authority = PublicationAuthorityClient(
                    settings, handler.broker, client=http
                )
                yield handler
        finally:
            authority.engine.dispose()
            token_path.unlink(missing_ok=True)

    def receipts(self) -> list[dict[str, Any]]:
        with self.metadata.sessions() as session:
            return [
                {**row.result_json, "status": row.status}
                for row in session.scalars(select(Step).where(Step.name == EFFECT_STEP))
            ]


@asynccontextmanager
async def approved_publication(
    tmp_path: Path, *, deployment_changed: bool = True
) -> AsyncIterator[ApprovedPublication]:
    values = await _child(tmp_path, issue_body=BODY)
    _, repository, _, metadata, _, _, child = values
    try:
        child["orchestration"].update(
            workflow_run_id=str(uuid4()), activity_id="publication-fixture", attempt=1
        )
        loaded = metadata._load_confirmed_finding(child)
        loaded = metadata._acquire_delivery_leases(loaded)
        assert loaded["status"] == "LEASES_ACQUIRED"
        branch = f"agent/falcone/42/{loaded['change_set_id']}"
        heads = {
            "falcone": "c" * 40,
            "falcone-charts": "d" * 40 if deployment_changed else loaded["deployment_base_sha"],
        }
        worktree_root = metadata.settings.worktree_root
        for slug in heads:
            (worktree_root / slug / loaded["change_set_id"]).mkdir(parents=True, mode=0o700)
        payload = {
            **loaded,
            "source_worktree": str(worktree_root / "falcone" / loaded["change_set_id"]),
            "deployment_worktree": str(worktree_root / "falcone-charts" / loaded["change_set_id"]),
            "source_branch": branch,
            "deployment_branch": branch,
            "openspec_identifier": "isolated-publication-authority",
            "change_evidence": {
                "source": {"head_sha": heads["falcone"], "changed_file_count": 1},
                "deployment": {
                    "head_sha": heads["falcone-charts"],
                    "changed_file_count": int(deployment_changed),
                },
            },
        }
        payload = metadata._persist_worktree_leases(payload)
        prepared = await metadata.handle("delivery.prepare_publication", payload)
        assert prepared["status"] == "PUBLICATION_READY", prepared.get("blocker")
        with metadata.sessions() as session:
            issue = session.scalar(select(BacklogIssueRecord))
            assert issue is not None
            issue_snapshot = {
                "number": issue.issue_number,
                "state": issue.state,
                "title": issue.title,
                "body": BODY,
                "updatedAt": issue.github_updated_at.replace(tzinfo=UTC).isoformat(),
                "url": issue.issue_url_reference,
            }
        yield ApprovedPublication(
            tmp_path, metadata, repository, branch, heads, issue_snapshot, payload, prepared
        )
    finally:
        _dispose(values)
