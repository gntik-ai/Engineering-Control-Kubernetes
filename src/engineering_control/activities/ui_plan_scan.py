"""NEW-123: scan-worker steps of the UI migration plan (flow A).

The inventory and kit snapshot are too large for a model prompt, so the scan worker that
produced them copies them (hash-verified) into a dedicated read-only input directory the
drafter and the reviewer may Read; drafts and review findings for the next model step are
staged there too, and the approved blueprint is published as evidence. No model runs here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

from engineering_control.config import Settings
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.safe_files import open_directory_no_follow, write_text_at
from engineering_control.engineering.ui_blueprint import blueprint_sha256
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    DEFAULT_BATCH_PAGES,
    FRAME,
    MAX_BATCH_PAGES,
    part_content_sha256,
    stage_plan,
)
from engineering_control.engineering.ui_inventory import (
    InventoryError,
    inventory_content_key,
    inventory_hash,
)
from engineering_control.engineering.ui_kit import snapshot_hash
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.manager import GitWorktreeManager

UI_PLAN_INPUT_DIRECTORY = "ui-plan-inputs"
# NEW-136: an inventory recorded before the legacy app tree has no stable reuse key.
UI_PLAN_INVENTORY_KEY_MISSING = "UI_PLAN_INVENTORY_KEY_MISSING"
UI_PLAN_SCAN_STEPS = frozenset(
    {"ui_plan.stage_inputs", "ui_plan.stage_document", "ui_plan.stage_parts"}
)
UI_PLAN_INPUT_DIRECTORY_MISSING = "UI_PLAN_INPUT_DIRECTORY_MISSING"
_DOCUMENT_NAME = re.compile(r"^(draft|review)-[12]\.json$")
# NEW-131: one part version or its review, e.g. part-b003-0123456789abcdef-draft-1.json.
# NEW-138: up to three drafts and reviews per part.
_PART_DOCUMENT_NAME = re.compile(
    r"^part-(frame|b[0-9]{3})-[a-f0-9]{16}-(draft|review)-[123]\.json$"
)
# NEW-131 review: the complete reviewed frame every batch drafter and reviewer reads.
_FRAME_BRIEF_NAME = re.compile(r"^part-frame-[a-f0-9]{16}-brief\.json$")
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MODEL_READERS = ("codex-runner", "claude-runner")


def _read_no_follow(root: Path, directory: Path, name: str) -> bytes:
    verified = open_directory_no_follow(root, directory)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=verified)
    finally:
        os.close(verified)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_INPUT_BYTES:
            raise ValueError(f"ui plan input {name} is not a bounded regular file")
        return handle.read()


def _digest_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(f"{field} must be a sha256 digest")
    return value


class UiPlanScanSteps:
    """Mixed into ScanActivityHandler (which provides settings, manager and evidence)."""

    settings: Settings
    manager: GitWorktreeManager

    def _publish_reports(  # pragma: no cover - provided by ScanActivityHandler
        self,
        payload: dict[str, Any],
        reports: list[Path],
        *,
        filename_prefix: str,
        category: str = "changes",
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _ui_plan_input_base(self) -> Path | None:
        """The bootstrap-provisioned input root (scan-runner owned); never created here.

        /srv/engineering/worktrees itself is root-owned 0711, so a missing root is an
        operator provisioning gap and fails closed instead of being created ad hoc."""
        base = self.settings.worktree_root / UI_PLAN_INPUT_DIRECTORY
        try:
            info = base.lstat()
        except FileNotFoundError:
            return None
        return base if stat.S_ISDIR(info.st_mode) else None

    @staticmethod
    def _ui_plan_missing_root() -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "blocker": UI_PLAN_INPUT_DIRECTORY_MISSING,
            "reason": f"provision /srv/engineering/worktrees/{UI_PLAN_INPUT_DIRECTORY} "
            "(scan-runner 0700, traverse ACL for codex-runner and claude-runner)",
        }

    def _ui_plan_input_directory(self, payload: dict[str, Any], base: Path) -> Path:
        return base / str(UUID(str(payload["blueprint_id"])))

    def _ui_plan_write(self, base: Path, directory: Path, name: str, text: str) -> dict[str, Any]:
        verified = open_directory_no_follow(base, directory)
        try:
            write_text_at(verified, name, text)
        finally:
            os.close(verified)
        data = text.encode("utf-8")
        return {
            "path": str(directory / name),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }

    def _ui_plan_grant(self, directory: Path) -> None:
        self.manager.grant_model_read(directory, _MODEL_READERS)

    def _ui_plan_verified_sources(
        self, payload: dict[str, Any]
    ) -> dict[str, tuple[str, bytes, dict[str, Any]]]:
        """label -> (file name, raw bytes, document) of the hash-verified inventory and kit."""
        inventory = _digest_name(payload.get("inventory_sha256"), "inventory_sha256")
        kit = _digest_name(payload.get("kit_snapshot_sha256"), "kit_snapshot_sha256")
        evidence = self.settings.evidence_root
        sources = (
            ("inventory", evidence / "scan" / "ui-inventory" / inventory[:32],
             "ui-inventory.json", "inventory_sha256", inventory),
            ("kit", evidence / "scan" / "ui-kit" / kit[:32],
             "ui-kit-snapshot.json", "snapshot_sha256", kit),
        )  # fmt: skip
        verified: dict[str, tuple[str, bytes, dict[str, Any]]] = {}
        for label, source_directory, name, field, digest in sources:
            raw = _read_no_follow(evidence / "scan", source_directory, name)
            document = json.loads(raw)
            # The embedded digest alone proves nothing: recompute it from the content the
            # same way its producer did (ui_inventory.inventory_hash, ui_kit.snapshot_hash).
            recompute = inventory_hash if label == "inventory" else snapshot_hash
            if (
                not isinstance(document, dict)
                or document.get(field) != digest
                or recompute(document) != digest
            ):
                raise ValueError(f"ui plan {label} evidence does not match its digest")
            verified[label] = (name, raw, document)
        return verified

    def _ui_plan_stage_inputs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Copy the hash-verified inventory and kit snapshot into the model input directory."""
        base = self._ui_plan_input_base()
        if base is None:
            return {**self._ui_plan_missing_root(), "blueprint_id": payload["blueprint_id"]}
        verified = self._ui_plan_verified_sources(payload)
        directory = self._ui_plan_input_directory(payload, base)
        files: dict[str, Any] = {}
        for label, (name, raw, _document) in verified.items():
            files[label] = self._ui_plan_write(base, directory, name, raw.decode("utf-8"))
        self._ui_plan_grant(directory)
        return {
            "status": "STAGED",
            "blueprint_id": payload["blueprint_id"],
            "input_directory": str(directory),
            "files": files,
        }

    def _ui_plan_stage_parts(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-131: fill the mechanical fields, plan the frame and the page batches, and
        stage each part's small input file (its inventory slice and the kit catalogue)."""
        batch_pages = payload.get("batch_pages")
        if batch_pages is None:
            # Inputs recorded before NEW-131 (a run that crossed the upgrade) carry no size.
            batch_pages = DEFAULT_BATCH_PAGES
        if type(batch_pages) is not int or not 1 <= batch_pages <= MAX_BATCH_PAGES:
            raise ValueError("ui plan batch size is invalid")
        base = self._ui_plan_input_base()
        if base is None:
            return {**self._ui_plan_missing_root(), "blueprint_id": payload["blueprint_id"]}
        verified = self._ui_plan_verified_sources(payload)
        try:
            # NEW-136: the parts' reuse identity, stable across unrelated legacy commits.
            content_key = inventory_content_key(verified["inventory"][2])
        except InventoryError as error:
            return {
                "status": "BLOCKED",
                "blocker": UI_PLAN_INVENTORY_KEY_MISSING,
                "reason": str(error)[:300],
                "blueprint_id": payload["blueprint_id"],
            }
        directory = self._ui_plan_input_directory(payload, base)
        plan = stage_plan(verified["inventory"][2], verified["kit"][2], batch_pages)
        written = {
            name: self._ui_plan_write(
                base, directory, name, json.dumps(document, indent=2, sort_keys=True) + "\n"
            )
            for name, document in plan["files"].items()
        }
        self._ui_plan_grant(directory)
        return {
            "status": "STAGED",
            "blueprint_id": payload["blueprint_id"],
            "mechanical": plan["mechanical"],
            "mechanical_sha256": plan["mechanical_sha256"],
            "inventory_content_key": content_key,
            # Carried on every part: the workflow forwards the parts to ui_plan.record_parts
            # verbatim, so the key reaches the controller without a workflow change.
            "parts": [
                {**part, "input": written[part["input"]], "content_key": content_key}
                for part in plan["parts"]
            ],
            "kit_slice": written[plan["kit_slice"]],
        }

    def _ui_plan_stage_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stage one draft or review for the next model step, or publish the blueprint."""
        document = payload.get("document")
        if not isinstance(document, dict):
            raise ValueError("ui plan document must be an object")
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if SecretRedactor().redact_text(text).value != text:
            raise ValueError("ui plan document is not redaction-stable")
        if not payload.get("publish"):
            name = str(payload.get("name", ""))
            part = _PART_DOCUMENT_NAME.fullmatch(name)
            if (
                not _DOCUMENT_NAME.fullmatch(name)
                and part is None
                and not _FRAME_BRIEF_NAME.fullmatch(name)
            ):
                raise ValueError("ui plan document name is invalid")
            base = self._ui_plan_input_base()
            if base is None:
                return self._ui_plan_missing_root()
            directory = self._ui_plan_input_directory(payload, base)
            staged = self._ui_plan_write(base, directory, name, text)
            if part is not None and part.group(2) == "draft":
                # NEW-131: Hermes's hash of exactly the part content the reviewer reads.
                written = json.loads(text)
                kind = FRAME if part.group(1) == FRAME else BATCH
                if not isinstance(written.get(kind), dict):
                    raise ValueError("a staged ui plan part draft carries no part")
                staged["document_sha256"] = canonical_sha256(written)
                staged["part_sha256"] = part_content_sha256(written[kind])
            elif name.startswith("draft-"):
                # Hermes's own record of exactly what the reviewer is given (not the model's).
                written = json.loads(text)
                if not isinstance(written.get("blueprint"), dict):
                    raise ValueError("a staged ui plan draft carries no blueprint")
                staged["document_sha256"] = canonical_sha256(written)
                staged["blueprint_sha256"] = blueprint_sha256(written["blueprint"])
            self._ui_plan_grant(directory)
            return {"status": "STAGED", "document": staged}
        markdown = payload.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            raise ValueError("a published ui blueprint needs its Markdown")
        blueprint_id = str(UUID(str(payload["blueprint_id"])))
        directory = self.settings.evidence_root / "scan" / "ui-blueprint" / blueprint_id
        written = [
            self._ui_plan_write_evidence(directory, "ui-blueprint.json", text),
            self._ui_plan_write_evidence(directory, "ui-blueprint.md", markdown),
        ]
        receipts = self._publish_reports(
            {"project": payload["project"], "orchestration": payload.get("orchestration")},
            [directory / "ui-blueprint.json", directory / "ui-blueprint.md"],
            filename_prefix=f"ui-blueprint-{blueprint_id[:8]}",
            category="snapshot",
        )
        return {"status": "PUBLISHED", "files": written, "evidence": receipts}

    def _ui_plan_write_evidence(self, directory: Path, name: str, text: str) -> dict[str, Any]:
        verified = open_directory_no_follow(self.settings.evidence_root / "scan", directory)
        try:
            write_text_at(verified, name, text)
        finally:
            os.close(verified)
        data = text.encode("utf-8")
        return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
