from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

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
        directory = self.root / project.value / campaign_id / category
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        target = directory / filename
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
        relative = target.relative_to(self.root).as_posix()
        from uuid import UUID

        return ArtifactMetadata(
            project=project,
            campaign_id=UUID(campaign_id),
            change_set_id=UUID(change_set_id) if change_set_id else None,
            run_id=UUID(run_id),
            relative_path=relative,
            sha256=hashlib.sha256(payload).hexdigest(),
            content_type="application/json",
            producer=producer,
            redaction_status=(
                RedactionStatus.REDACTED if report.replacements else RedactionStatus.NOT_REQUIRED
            ),
            size_bytes=len(payload),
        )
