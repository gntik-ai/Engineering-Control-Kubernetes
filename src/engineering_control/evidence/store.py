from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from engineering_control.models.core import ArtifactMetadata
from engineering_control.models.enums import Project, RedactionStatus
from engineering_control.security.redaction import SecretRedactor


class LocalEvidenceStore:
    def __init__(self, root: Path = Path("/srv/engineering/evidence")) -> None:
        self.root = root
        self.redactor = SecretRedactor()

    def put_json(
        self,
        *,
        project: Project,
        campaign_id: str,
        category: str,
        filename: str,
        value: Any,
        run_id: str,
        producer: str,
        change_set_id: str | None = None,
    ) -> ArtifactMetadata:
        if category not in {"snapshot", "findings", "changes"}:
            raise ValueError("invalid evidence category")
        if Path(filename).name != filename:
            raise ValueError("evidence filename must not contain path components")
        report = self.redactor.redact(value)
        payload = json.dumps(report.value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        return self.put_bytes(
            artifact_id=None,
            project=project,
            campaign_id=UUID(campaign_id),
            change_set_id=UUID(change_set_id) if change_set_id else None,
            category=category,
            filename=filename,
            payload=payload,
            run_id=UUID(run_id),
            producer=producer,
            content_type="application/json",
            redaction_status=(
                RedactionStatus.REDACTED if report.replacements else RedactionStatus.NOT_REQUIRED
            ),
        )

    def put_bytes(
        self,
        *,
        artifact_id: UUID | None,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
        category: str,
        filename: str,
        payload: bytes,
        run_id: UUID,
        producer: str,
        content_type: str,
        redaction_status: RedactionStatus,
    ) -> ArtifactMetadata:
        if category not in {"snapshot", "findings", "changes"}:
            raise ValueError("invalid evidence category")
        if Path(filename).name != filename:
            raise ValueError("evidence filename must not contain path components")
        if campaign_id is not None:
            owner_path = str(campaign_id)
        elif change_set_id is not None:
            owner_path = f"changesets/{change_set_id}"
        else:
            owner_path = f"runs/{run_id}"
        directory = self.root / project.value / owner_path / category
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        identity = artifact_id or UUID(bytes=hashlib.sha256(payload).digest()[:16])
        target = directory / f"{identity}-{filename}"
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
        relative = target.relative_to(self.root).as_posix()
        return ArtifactMetadata(
            artifact_id=identity,
            project=project,
            campaign_id=campaign_id,
            change_set_id=change_set_id,
            run_id=run_id,
            relative_path=relative,
            sha256=hashlib.sha256(payload).hexdigest(),
            content_type=content_type,
            producer=producer,
            redaction_status=redaction_status,
            size_bytes=len(payload),
        )
