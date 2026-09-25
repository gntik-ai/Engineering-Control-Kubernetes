from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.evidence.ingest import create_evidence_ingest_app
from engineering_control.evidence.transfer import EvidenceEnvelope, EvidencePublisher
from engineering_control.models.core import ArtifactMetadata
from engineering_control.models.enums import Project, RedactionStatus


class FakeEvidenceRepository:
    def __init__(self, workflow_id: str, run_id: UUID, campaign_id: UUID) -> None:
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.campaign_id = campaign_id
        self.persisted: list[ArtifactMetadata] = []
        self.heartbeats: list[tuple[str, str]] = []

    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        if workflow_id != self.workflow_id or project is not Project.FALCONE:
            raise ValueError("binding mismatch")
        if campaign_id != self.campaign_id or change_set_id is not None:
            raise ValueError("scope mismatch")
        return {
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "change_set_id": None,
        }

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        self.persisted.append(metadata)
        return {"id": str(metadata.artifact_id), **metadata.model_dump(mode="json")}

    async def persist_worker_heartbeat(self, identity: str, hostname: str) -> dict[str, Any]:
        self.heartbeats.append((identity, hostname))
        return {
            "identity": identity,
            "hostname": hostname,
            "observed_at": "2026-08-22T00:00:00+00:00",
        }


def envelope_headers(
    payload: bytes,
    *,
    artifact_id: UUID,
    workflow_id: str,
    campaign_id: UUID,
) -> dict[str, str]:
    envelope = EvidenceEnvelope(
        artifact_id=artifact_id,
        project=Project.FALCONE,
        workflow_id=workflow_id,
        campaign_id=campaign_id,
        category="findings",
        filename="scanner.json",
        content_type="application/json",
        producer="scan-runner",
        redaction_status=RedactionStatus.REDACTED,
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return {
        **envelope.headers(),
        "Authorization": f"Bearer {'e' * 48}",
        "X-Engineering-Actor": "scan-runner",
    }


def test_private_ingest_stores_authorized_pre_redacted_evidence(tmp_path: Path) -> None:
    workflow_id = "audit-falcone-test"
    campaign_id = uuid4()
    repository = FakeEvidenceRepository(workflow_id, uuid4(), campaign_id)
    settings = Settings(evidence_root=tmp_path / "evidence", evidence_max_bytes=4096)
    payload = json.dumps({"status": "PASS", "findings": []}).encode()
    artifact_id = uuid4()
    app = create_evidence_ingest_app(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        tokens={"scan-runner": "e" * 48},
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/evidence",
            headers=envelope_headers(
                payload,
                artifact_id=artifact_id,
                workflow_id=workflow_id,
                campaign_id=campaign_id,
            ),
            content=payload,
        )

    assert response.status_code == 201
    assert response.json()["id"] == str(artifact_id)
    assert repository.persisted[0].sha256 == hashlib.sha256(payload).hexdigest()
    stored = list((tmp_path / "evidence").rglob("*.json"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == payload


def test_ingest_rejects_unredacted_text_before_persistence(tmp_path: Path) -> None:
    workflow_id = "audit-falcone-redaction"
    campaign_id = uuid4()
    repository = FakeEvidenceRepository(workflow_id, uuid4(), campaign_id)
    fake_token = "".join(("gh", "p_", "A" * 36))
    payload = json.dumps({"token": fake_token}).encode()
    app = create_evidence_ingest_app(
        settings=Settings(evidence_root=tmp_path / "evidence"),
        repository=repository,  # type: ignore[arg-type]
        tokens={"scan-runner": "e" * 48},
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/evidence",
            headers=envelope_headers(
                payload,
                artifact_id=uuid4(),
                workflow_id=workflow_id,
                campaign_id=campaign_id,
            ),
            content=payload,
        )

    assert response.status_code == 422
    assert not repository.persisted
    assert not (tmp_path / "evidence").exists()


def test_ingest_rejects_worker_producer_impersonation(tmp_path: Path) -> None:
    workflow_id = "audit-falcone-identity"
    campaign_id = uuid4()
    repository = FakeEvidenceRepository(workflow_id, uuid4(), campaign_id)
    payload = b'{"status":"PASS"}'
    headers = envelope_headers(
        payload,
        artifact_id=uuid4(),
        workflow_id=workflow_id,
        campaign_id=campaign_id,
    )
    headers["X-Engineering-Actor"] = "codex-runner"
    app = create_evidence_ingest_app(
        settings=Settings(evidence_root=tmp_path / "evidence"),
        repository=repository,  # type: ignore[arg-type]
        tokens={"codex-runner": "e" * 48, "scan-runner": "s" * 48},
    )

    with TestClient(app) as client:
        response = client.post("/v1/evidence", headers=headers, content=payload)

    assert response.status_code == 403
    assert not repository.persisted


def test_worker_heartbeat_identity_comes_from_its_bearer_token(tmp_path: Path) -> None:
    repository = FakeEvidenceRepository("unused", uuid4(), uuid4())
    app = create_evidence_ingest_app(
        settings=Settings(evidence_root=tmp_path / "evidence"),
        repository=repository,  # type: ignore[arg-type]
        tokens={"scan-runner": "s" * 48},
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/workers/heartbeat",
            headers={
                "Authorization": f"Bearer {'s' * 48}",
                "X-Engineering-Actor": "scan-runner",
                "X-Worker-Hostname": "hermes-worker",
            },
        )
        spoofed = client.post(
            "/v1/workers/heartbeat",
            headers={
                "Authorization": f"Bearer {'s' * 48}",
                "X-Engineering-Actor": "release-runner",
                "X-Worker-Hostname": "hermes-worker",
            },
        )
    assert response.status_code == 202
    assert repository.heartbeats == [("scan-runner", "hermes-worker")]
    assert spoofed.status_code == 403


def test_delivery_worker_heartbeat_is_allowlisted(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'heartbeat.sqlite'}")
    Base.metadata.create_all(repository.engine)
    app = create_evidence_ingest_app(
        settings=Settings(evidence_root=tmp_path / "evidence"),
        repository=repository,
        tokens={"delivery-runner": "d" * 48},
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/workers/heartbeat",
            headers={
                "Authorization": f"Bearer {'d' * 48}",
                "X-Engineering-Actor": "delivery-runner",
                "X-Worker-Hostname": "worker-test",
            },
        )
    assert response.status_code == 202
    assert response.json()["identity"] == "delivery-runner"


def test_github_worker_heartbeat_is_allowlisted(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'github-heartbeat.sqlite'}")
    Base.metadata.create_all(repository.engine)
    app = create_evidence_ingest_app(
        settings=Settings(evidence_root=tmp_path / "evidence"),
        repository=repository,
        tokens={"github-runner": "g" * 48},
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/workers/heartbeat",
            headers={
                "Authorization": f"Bearer {'g' * 48}",
                "X-Engineering-Actor": "github-runner",
                "X-Worker-Hostname": "controller-test",
            },
        )
    assert response.status_code == 202
    assert response.json()["identity"] == "github-runner"


def test_publisher_uses_private_origin_and_restricted_token_file(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    report = evidence_root / "report.json"
    report.write_text('{"status":"PASS"}\n', encoding="utf-8")
    token_file = tmp_path / "evidence.token"
    token_file.write_text("t" * 48, encoding="utf-8")
    os.chmod(token_file, 0o640)

    def receive(request: httpx.Request) -> httpx.Response:
        artifact_id = request.headers["X-Evidence-Artifact-Id"]
        assert request.url == httpx.URL("http://10.10.0.2:8750/v1/evidence")
        assert request.headers["Authorization"] == f"Bearer {'t' * 48}"
        return httpx.Response(201, json={"id": artifact_id})

    publisher = EvidencePublisher(
        Settings(
            environment="production",
            evidence_ingest_url="http://10.10.0.2:8750/v1/evidence",
            evidence_ingest_token_file=token_file,
        ),
        evidence_root,
        httpx.MockTransport(receive),
    )
    result = publisher.publish(
        report,
        project=Project.FALCONE,
        workflow_id="audit-falcone-publish",
        campaign_id=uuid4(),
        category="findings",
        filename="report.json",
        producer="scan-runner",
        content_type="application/json",
        redaction_status=RedactionStatus.REDACTED,
    )

    assert UUID(str(result["id"]))


def test_publisher_rejects_public_ingest_origin(tmp_path: Path) -> None:
    try:
        EvidencePublisher(
            Settings(evidence_ingest_url="https://8.8.8.8/v1/evidence"),
            tmp_path,
        )
    except ValueError as error:
        assert "public" in str(error)
    else:
        raise AssertionError("public evidence origin was accepted")


def test_scan_reports_pass_the_real_ingest_redaction_check(tmp_path: Path) -> None:
    # NEW-23: delivery prechecks uploaded raw scanner reports asserting REDACTED, and the
    # ingest service's central re-redaction answered 422, failing the first live delivery.
    # Also covers redaction idempotency: already-redacted "password: [REDACTED]" text
    # used to be re-matched and rejected even after correct pre-redaction.
    from engineering_control.activities.scan import ScanActivityHandler

    workflow_id = "backlog-loop-falcone-prechecks"
    campaign_id = uuid4()
    repository = FakeEvidenceRepository(workflow_id, uuid4(), campaign_id)
    evidence_root = tmp_path / "evidence"
    reports = evidence_root / "scan" / "prechecks"
    reports.mkdir(parents=True)
    trivy = reports / "trivy.json"
    trivy.write_text(
        json.dumps(
            {
                "Results": [
                    {
                        "Class": "secret",
                        "Secrets": [{"RuleID": "generic", "Match": "password: hunter2"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    semgrep = reports / "semgrep.txt"
    semgrep.write_text("config.yaml:3\n  password: hunter2  # fixture\n", encoding="utf-8")
    already = reports / "already-redacted.txt"
    already.write_text("password: [REDACTED]\nCookie: [REDACTED]\n", encoding="utf-8")
    token_file = tmp_path / "evidence.token"
    token_file.write_text("e" * 48, encoding="utf-8")
    os.chmod(token_file, 0o640)
    app = create_evidence_ingest_app(
        settings=Settings(evidence_root=tmp_path / "stored"),
        repository=repository,  # type: ignore[arg-type]
        tokens={"scan-runner": "e" * 48},
    )

    with TestClient(app) as client:

        def forward(request: httpx.Request) -> httpx.Response:
            response = client.post(
                "/v1/evidence", headers=dict(request.headers), content=request.content
            )
            return httpx.Response(response.status_code, content=response.content)

        settings = Settings(
            evidence_root=evidence_root,
            evidence_ingest_url="http://10.10.0.2:8750/v1/evidence",
            evidence_ingest_token_file=token_file,
        )
        handler = ScanActivityHandler(
            settings,
            publisher=EvidencePublisher(settings, evidence_root, httpx.MockTransport(forward)),
        )
        receipts = handler._publish_reports(
            {
                "project": "falcone",
                "campaign_id": str(campaign_id),
                "orchestration": {"workflow_id": workflow_id},
            },
            [already, semgrep, trivy],
            filename_prefix="prechecks-source",
        )

    assert len(receipts) == 3
    assert len(repository.persisted) == 3
    stored = "".join(path.read_text() for path in (tmp_path / "stored").rglob("*.*"))
    assert "hunter2" not in stored
    assert "# fixture" in stored


def test_empty_evidence_is_recorded_not_uploaded(tmp_path: Path) -> None:
    """NEW-54: a source-only ChangeSet's deployment patch is empty; uploading it raised
    'evidence upload is empty or exceeds the configured size bound' in validate_changeset."""
    from engineering_control.activities.scan import ScanActivityHandler

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    empty = evidence_root / "deployment.patch"
    empty.write_text("", encoding="utf-8")

    class _RefusingPublisher:
        def upload(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("an empty report must not be uploaded")

    handler = ScanActivityHandler(
        Settings(evidence_root=evidence_root),
        publisher=_RefusingPublisher(),  # type: ignore[arg-type]
    )

    receipts = handler._publish_reports(
        {"project": "musematic", "orchestration": {"workflow_id": "wf-1"}},
        [empty],
        filename_prefix="postchecks-deployment",
    )

    assert receipts == [{"status": "EMPTY_NOT_UPLOADED", "filename": "deployment.patch"}]
