"""Read-only NEW-23 diagnostic: why does the evidence ingest service answer 422?

Runs the ingest service's own two checks outside the HTTP path so their real error
messages are visible (the publisher drops the 422 body):

  controller:  sudo -u engineering-control /opt/engineering-control/venv/bin/python \
                 diagnose-new23.py binding
  worker:      sudo /opt/engineering-control/venv/bin/python diagnose-new23.py reports

Writes nothing. Prints only check outcomes, file names, sizes and hashes -- never content.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

WORKFLOW_ID = (
    "backlog-loop-musematic-8034e576-573f-4f82-b674-ec5134b2dd7a-"
    "364b6f4f-7180-4f0d-a124-9e6e6677f9ec-issue-1-attempt-1"
)
CHANGE_SET_ID = UUID("6f281a39-e695-525b-8992-177fc47ba445")
PRECHECKS = Path("/srv/engineering/evidence/scan") / str(CHANGE_SET_ID) / "prechecks"


def binding() -> None:
    from engineering_control.config import get_settings
    from engineering_control.db.repository import SQLMetadataRepository
    from engineering_control.models.enums import Project

    settings = get_settings()
    repository = SQLMetadataRepository(settings.database_url.get_secret_value())
    try:
        result = asyncio.run(
            repository.resolve_evidence_binding(
                WORKFLOW_ID, Project.MUSEMATIC, None, CHANGE_SET_ID
            )
        )
    except ValueError as error:
        print(f"BINDING_REJECTED: {error}")
        return
    print(f"BINDING_OK: {json.dumps({k: str(v) for k, v in result.items()})}")


def reports() -> None:
    import engineering_control.activities.scan as scan
    from engineering_control.config import get_settings
    from engineering_control.evidence.store import LocalEvidenceStore
    from engineering_control.evidence.transfer import EvidenceIngestService
    from engineering_control.models.enums import RedactionStatus

    print(f"scan module: {scan.__file__}")
    print(f"scan redacts before upload: {'redactor.redact(parsed)' in Path(scan.__file__).read_text()}")
    settings = get_settings()
    print(f"evidence_max_bytes: {settings.evidence_max_bytes}")
    service = EvidenceIngestService(
        None,  # type: ignore[arg-type]
        LocalEvidenceStore(Path("/nonexistent")),
        max_bytes=settings.evidence_max_bytes,
    )

    class Envelope:
        redaction_status = RedactionStatus.REDACTED

    if not PRECHECKS.is_dir():
        print(f"NO_REPORTS: {PRECHECKS} does not exist")
        return
    for report in sorted(PRECHECKS.rglob("*")):
        if not report.is_file():
            continue
        payload = report.read_bytes()
        envelope = Envelope()
        envelope.content_type = "application/json" if report.suffix == ".json" else "text/plain"  # type: ignore[attr-defined]
        try:
            service._require_pre_redacted(envelope, payload)  # type: ignore[arg-type]
            verdict = "ACCEPTED"
        except ValueError as error:
            verdict = f"REJECTED: {error}"
        size_note = " OVER_MAX" if len(payload) > settings.evidence_max_bytes else ""
        print(
            f"{report.relative_to(PRECHECKS)} bytes={len(payload)}{size_note} "
            f"sha256={hashlib.sha256(payload).hexdigest()[:12]} -> {verdict}"
        )


if __name__ == "__main__":
    {"binding": binding, "reports": reports}[sys.argv[1]]()
