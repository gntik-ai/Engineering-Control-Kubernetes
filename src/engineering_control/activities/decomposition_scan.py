"""NEW-234 PR5: scan-worker steps of the backlog decomposition plan (flow D).

Modelled on ui_plan_scan.py. The scan worker reads the project's documentation from the
immutable snapshot at the pinned source SHA and, with the read-only GitHub issue snapshot
the GitHub worker took, builds the deterministic inventory (PR 4); it stages each part's
input file, and the drafts and reviews the next model step reads, into the model input
directory (each at most 64 KiB, design section 3); and it publishes the candidate plan as
evidence. No model runs here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from engineering_control.activities.ui_plan_scan import UiPlanScanSteps
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.decomposition import (
    EPIC,
    FRAME,
    PROJECT,
    part_content_sha256,
    plan_json,
)
from engineering_control.engineering.decomposition_inventory import (
    INPUT_TOO_LARGE,
    SOURCE_INVALID,
    DecompositionSourceError,
    IssueSnapshot,
    build_inventory,
    read_sources,
    stage_files,
)
from engineering_control.security.redaction import SecretRedactor

DECOMPOSITION_SCAN_STEPS = frozenset(
    {"decomposition.inventory", "decomposition.stage_inputs", "decomposition.stage_document"}
)
# The inventory crosses the workflow once (scan worker -> controller); bounded well below
# Temporal's payload limit.
MAX_INVENTORY_BYTES = 1_500_000
MAX_ISSUES = 300
MAX_ISSUE_BODY = 65_536
_SHA = re.compile(r"^[a-f0-9]{40}$")
_DOCUMENT_NAME = re.compile(r"^part-(frame|ep[0-9]{2})-[a-f0-9]{16}-(draft|review)-[123]\.json$")


def _issue(item: Any) -> IssueSnapshot:
    if not isinstance(item, dict):
        raise DecompositionSourceError(SOURCE_INVALID, ["an issue of the snapshot is invalid"])
    number, parent = item.get("number"), item.get("parent")
    title, body, state = item.get("title"), item.get("body"), item.get("state")
    labels = item.get("labels") or []
    if (
        type(number) is not int
        or (parent is not None and type(parent) is not int)
        or not isinstance(title, str)
        or not isinstance(body, str)
        or len(body) > MAX_ISSUE_BODY
        or state not in {"OPEN", "CLOSED"}
        or not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
    ):
        raise DecompositionSourceError(SOURCE_INVALID, [f"issue {number!r} is invalid"])
    return IssueSnapshot(
        number=number,
        title=title,
        body=body,
        state=state,
        labels=tuple(labels),
        parent=parent,
    )


class DecompositionScanSteps(UiPlanScanSteps):
    """Mixed into ScanActivityHandler (which provides settings, manager and evidence). The
    model input root is the provisioned UI plan input root (one directory per plan id)."""

    def _decomposition_inventory(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("project") != PROJECT:
            raise ValueError("decomposition inventory is configured only for falcone-serverless")
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("decomposition inventory requires the immutable source snapshot")
        source_sha = str(snapshot.get("source_sha") or "")
        if not _SHA.fullmatch(source_sha):
            raise ValueError("decomposition source SHA is invalid")
        worktree = Path(str(snapshot["source_worktree"]))
        # The snapshot must be clean and exactly at the pinned commit.
        self.manager.inspect_snapshot(worktree, expected_sha=source_sha)
        raw = payload.get("issues")
        try:
            if not isinstance(raw, list) or len(raw) > MAX_ISSUES:
                raise DecompositionSourceError(SOURCE_INVALID, ["the issue snapshot is invalid"])
            issues = [_issue(item) for item in raw]
            inventory = build_inventory(
                source_sha=source_sha, files=read_sources(worktree), issues=issues
            )
        except DecompositionSourceError as error:
            return {"status": "BLOCKED", "blocker": error.code, "blockers": error.details}
        if SecretRedactor().redact(inventory).value != inventory:
            # The controller receives the inventory through the redacting activity boundary;
            # credential-like text would change it in transit, so fail closed here, early.
            return {
                "status": "BLOCKED",
                "blocker": SOURCE_INVALID,
                "blockers": ["the sources hold credential-like text (not redaction-stable)"],
            }
        size = len(json.dumps(inventory, ensure_ascii=False).encode("utf-8"))
        if size > MAX_INVENTORY_BYTES:
            return {
                "status": "BLOCKED",
                "blocker": INPUT_TOO_LARGE,
                "blockers": [f"the inventory is {size} bytes, over {MAX_INVENTORY_BYTES}"],
            }
        return {
            "status": "INVENTORY_PASS",
            "inventory": inventory,
            "inventory_sha256": inventory["inventory_sha256"],
            "summary": {
                "epics": len(inventory["epics"]),
                "stories": len(inventory["stories"]),
                "references": len(inventory["references"]),
                "bytes": size,
            },
        }

    def _decomposition_directory(self, payload: dict[str, Any], base: Path) -> Path:
        return base / str(UUID(str(payload["decomposition_id"])))

    def _decomposition_stage(
        self, payload: dict[str, Any], files: dict[str, str]
    ) -> dict[str, Any]:
        base = self._ui_plan_input_base()
        if base is None:
            return self._ui_plan_missing_root()
        for name, text in files.items():
            if SecretRedactor().redact_text(text).value != text:
                return {
                    "status": "BLOCKED",
                    "blocker": SOURCE_INVALID,
                    "blockers": [f"{name} is not redaction-stable"],
                }
        directory = self._decomposition_directory(payload, base)
        try:
            receipts = stage_files(base, directory, files)
        except DecompositionSourceError as error:
            return {"status": "BLOCKED", "blocker": error.code, "blockers": error.details}
        self._ui_plan_grant(directory)
        return {"status": "STAGED", "input_directory": str(directory), "files": receipts}

    def _decomposition_stage_inputs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stage one part's input file (and, for the frame, the reference documents)."""
        files = payload.get("files")
        if (
            not isinstance(files, dict)
            or not files
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
        ):
            raise ValueError("decomposition input files must map names to text")
        return self._decomposition_stage(payload, dict(files))

    def _decomposition_stage_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stage one part draft or review for the next model step, or publish the plan."""
        document = payload.get("document")
        if not isinstance(document, dict):
            raise ValueError("decomposition document must be an object")
        if payload.get("publish"):
            return self._decomposition_publish(payload, document)
        name = str(payload.get("name") or "")
        match = _DOCUMENT_NAME.fullmatch(name)
        if match is None:
            raise ValueError("decomposition document name is invalid")
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        staged = self._decomposition_stage(payload, {name: text})
        if staged["status"] != "STAGED":
            return staged
        receipt = dict(staged["files"][name])
        if match.group(2) == "draft":
            # Hermes's hash of exactly the part content the reviewer reads.
            written = json.loads(text)
            kind = FRAME if match.group(1) == FRAME else EPIC
            if not isinstance(written.get(kind), dict):
                raise ValueError("a staged decomposition draft carries no part")
            receipt["document_sha256"] = canonical_sha256(written)
            receipt["part_sha256"] = part_content_sha256(written[kind])
        return {"status": "STAGED", "document": receipt}

    def _decomposition_publish(
        self, payload: dict[str, Any], plan: dict[str, Any]
    ) -> dict[str, Any]:
        markdown = payload.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            raise ValueError("a published decomposition plan needs its Markdown")
        text = plan_json(plan)
        if SecretRedactor().redact_text(text).value != text:
            raise ValueError("the decomposition plan is not redaction-stable")
        identity = str(UUID(str(payload["decomposition_id"])))
        directory = self.settings.evidence_root / "scan" / "backlog-decomposition" / identity
        written = [
            self._ui_plan_write_evidence(directory, "backlog-decomposition.json", text),
            self._ui_plan_write_evidence(directory, "backlog-decomposition.md", markdown),
        ]
        receipts = self._publish_reports(
            {"project": payload["project"], "orchestration": payload.get("orchestration")},
            [directory / "backlog-decomposition.json", directory / "backlog-decomposition.md"],
            filename_prefix=f"backlog-decomposition-{identity[:8]}",
            category="snapshot",
        )
        return {"status": "PUBLISHED", "files": written, "evidence": receipts}
