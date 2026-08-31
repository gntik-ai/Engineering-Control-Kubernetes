#!/usr/bin/env python3
"""Apply one typed operator-approved backlog policy request as root."""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from engineering_control.config import Settings
from engineering_control.db.models import GitHubBacklogCampaign
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BacklogApprovalBinding,
    canonical_sha256,
    is_reauthenticated_backlog_operator,
)
from engineering_control.policy.github_write import GitHubWritePolicyDocument

SAFE_REPOSITORIES = (
    "gntik-ai/falcone",
    "gntik-ai/falcone-charts",
    "gntik-ai/musematic",
    "gntik-ai/musematic-deploy",
    "gntik-ai/llmwiki",
)
APPROVAL_REQUIRED_STATUSES = frozenset(
    {
        "APPROVED_POLICY_PENDING",
        "APPROVED_INVENTORY",
        "APPROVED",
        "RUNNING",
        "PAUSED",
        "COMPLETE",
        "STOPPED",
        "CANCELLED",
        "EXPIRED",
    }
)
REQUEST_KEYS = frozenset(
    {
        "action",
        "backlog_campaign_id",
        "project",
        "approval_binding",
        "safe_repositories",
        "requested_by",
        "requested_at",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--expire-only", action="store_true")
    mode.add_argument("--audit-only", action="store_true")
    parser.add_argument(
        "--request",
        type=Path,
        default=Path("/var/lib/engineering-control/github-policy-requests/activate.json"),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("/etc/engineering-control/policy/github-issue-write-validation.yaml"),
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/var/lib/engineering-control/github-policy-backups"),
    )
    return parser.parse_args()


def secure_regular(path: Path, *, owner: int, mode: int) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"{path.name} must be a regular non-symlink file")
    if metadata.st_uid != owner or stat.S_IMODE(metadata.st_mode) != mode:
        raise ValueError(f"{path.name} ownership or mode is invalid")


def read_request(path: Path, api_uid: int) -> dict[str, Any]:
    secure_regular(path, owner=api_uid, mode=0o600)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS:
        raise ValueError("GitHub policy request schema is invalid")
    if value["action"] not in {
        "enable-existing-backlog",
        "disable-existing-backlog",
    }:
        raise ValueError("GitHub policy action is not allowlisted")
    if value["project"] not in {"falcone", "musematic", "llmwiki"}:
        raise ValueError("GitHub policy project is invalid")
    expected_repositories = (
        SAFE_REPOSITORIES if value["action"] == "enable-existing-backlog" else ()
    )
    if tuple(value["safe_repositories"]) != expected_repositories:
        raise ValueError("GitHub policy request repository set changed")
    requested_by = str(value["requested_by"])
    if not is_reauthenticated_backlog_operator(requested_by):
        raise ValueError("GitHub policy request lacks a typed reauthenticated operator")
    UUID(str(value["backlog_campaign_id"]))
    if len(str(value["approval_binding"])) != 64:
        raise ValueError("GitHub policy approval binding is invalid")
    datetime.fromisoformat(str(value["requested_at"]))
    return value


def validate_campaign(session: Session, request: dict[str, Any]) -> GitHubBacklogCampaign:
    requested_by = request.get("requested_by")
    if not isinstance(requested_by, str) or not is_reauthenticated_backlog_operator(requested_by):
        raise ValueError("GitHub policy request lacks a typed reauthenticated operator")
    campaign = session.get(GitHubBacklogCampaign, UUID(str(request["backlog_campaign_id"])))
    if (
        campaign is None
        or campaign.project != request["project"]
        or campaign.approval_binding != request["approval_binding"]
    ):
        raise ValueError("GitHub policy request has no current approved campaign")
    expected_statuses = (
        {"APPROVED_POLICY_PENDING"}
        if request["action"] == "enable-existing-backlog"
        else {"CANCELLED", "COMPLETE", "STOPPED"}
    )
    if campaign.status not in expected_statuses:
        raise ValueError("GitHub policy request campaign state is invalid")
    if request["action"] == "enable-existing-backlog":
        other = session.scalar(
            select(GitHubBacklogCampaign.id).where(
                GitHubBacklogCampaign.id != campaign.id,
                GitHubBacklogCampaign.status.in_({"APPROVED", "RUNNING", "PAUSED"}),
            )
        )
        if other is not None:
            raise ValueError("another backlog campaign already owns GitHub write policy")
    nested = "binding" in campaign.approval_json or "approval" in campaign.approval_json
    raw_binding = campaign.approval_json.get("binding")
    raw_approval = campaign.approval_json.get("approval")
    if nested and (not isinstance(raw_binding, dict) or not isinstance(raw_approval, dict)):
        raise ValueError("GitHub campaign nested approval evidence is malformed")
    binding = raw_binding if nested else campaign.approval_json
    approval = raw_approval if nested else campaign.approval_json
    if campaign.approval_json.get("policy_role") not in {None, "owner"}:
        raise ValueError("GitHub campaign approval evidence is incomplete")
    try:
        exact_binding = BacklogApprovalBinding.model_validate(
            {field: binding.get(field) for field in BacklogApprovalBinding.model_fields}
        )
    except ValueError as error:
        raise ValueError("GitHub campaign approval evidence is incomplete") from error
    if canonical_sha256(exact_binding.model_dump(mode="json")) != request["approval_binding"]:
        raise ValueError("GitHub campaign approval binding changed")
    approved_by = approval.get("approved_by")
    if not isinstance(approved_by, str) or not is_reauthenticated_backlog_operator(approved_by):
        raise ValueError("GitHub campaign approval actor is noncanonical")
    if approved_by != request["requested_by"]:
        raise ValueError("GitHub policy request actor changed")
    if approval.get("typed_confirmation") != BACKLOG_APPROVAL_PHRASE or not approval.get(
        "approved_at"
    ):
        raise ValueError("GitHub campaign approval evidence is incomplete")
    expires_at = exact_binding.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if request["action"] == "enable-existing-backlog" and expires_at <= datetime.now(UTC):
        raise ValueError("GitHub campaign approval expired")
    expected_pair = {
        "falcone": ["gntik-ai/falcone", "gntik-ai/falcone-charts"],
        "musematic": ["gntik-ai/musematic", "gntik-ai/musematic-deploy"],
        "llmwiki": ["gntik-ai/llmwiki", "gntik-ai/llmwiki"],
    }[campaign.project]
    if binding.get("paired_repositories") != expected_pair:
        raise ValueError("GitHub campaign pair changed")
    if binding.get("production") is not False:
        raise ValueError("GitHub campaign unexpectedly permits production")
    return campaign


def expire_campaign_approvals(session: Session, now: datetime) -> bool:
    """Expire all approval rows and report whether shared policy must be disabled."""
    active = session.scalars(
        select(GitHubBacklogCampaign).where(
            GitHubBacklogCampaign.status.in_(
                {
                    "APPROVED_POLICY_PENDING",
                    "APPROVED_INVENTORY",
                    "APPROVED",
                    "RUNNING",
                    "PAUSED",
                }
            )
        )
    ).all()
    unexpired_owner = False
    for campaign in active:
        raw_binding = campaign.approval_json.get("binding")
        binding = raw_binding if isinstance(raw_binding, dict) else campaign.approval_json
        try:
            expires_at = datetime.fromisoformat(str(binding.get("expires_at")))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            expires_at = None
        if expires_at is None or expires_at <= now:
            campaign.status = "EXPIRED"
            continue
        if campaign.status in {
            "APPROVED_POLICY_PENDING",
            "APPROVED",
            "RUNNING",
            "PAUSED",
        } and campaign.approval_json.get("policy_role") in {None, "owner"}:
            unexpired_owner = True
    if not unexpired_owner:
        for campaign in active:
            if campaign.approval_json.get("policy_role") == "inventory":
                campaign.status = "EXPIRED"
    return not unexpired_owner


def audit_approval_actors(session: Session) -> tuple[int, int, int]:
    """Fail closed when durable backlog approvals contain a noncanonical actor."""
    campaign_count = 0
    reviewed = 0
    unapproved = 0
    campaigns = session.scalars(select(GitHubBacklogCampaign)).yield_per(100)
    for campaign in campaigns:
        campaign_count += 1
        evidence = campaign.approval_json
        if not isinstance(evidence, dict):
            raise ValueError("durable backlog approval evidence is malformed")
        nested = "approval" in evidence
        raw_approval = evidence.get("approval")
        has_flat_approval = any(
            field in evidence for field in ("approved_by", "approved_at", "typed_confirmation")
        )
        if not nested and not has_flat_approval:
            if campaign.status in APPROVAL_REQUIRED_STATUSES:
                raise ValueError("durable backlog campaign lacks canonical approval evidence")
            unapproved += 1
            continue
        if nested and not isinstance(raw_approval, dict):
            raise ValueError("durable backlog approval evidence is malformed")
        approval = raw_approval if nested else evidence
        if nested and has_flat_approval:
            for field in ("approved_by", "approved_at", "typed_confirmation"):
                if field in evidence and evidence[field] != approval.get(field):
                    raise ValueError("durable backlog approval shapes disagree")
        actor = approval.get("approved_by")
        reviewed += 1
        if not isinstance(actor, str) or not is_reauthenticated_backlog_operator(actor):
            raise ValueError("durable backlog approval contains a noncanonical operator actor")
    return campaign_count, reviewed, unapproved


def validate_current_policy(path: Path, group_id: int) -> None:
    metadata = path.lstat()
    parent = path.parent.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != 0
        or metadata.st_gid != group_id
        or stat.S_IMODE(metadata.st_mode) != 0o640
        or parent.st_uid != 0
        or parent.st_gid != group_id
        or stat.S_IMODE(parent.st_mode) != 0o750
    ):
        raise ValueError("canonical GitHub policy metadata is unsafe")
    value = GitHubWritePolicyDocument.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    if value.enabled and tuple(value.safe_repositories) != SAFE_REPOSITORIES:
        raise ValueError("existing enabled GitHub policy is outside this mission")
    if not value.enabled and value.safe_repositories:
        raise ValueError("disabled GitHub policy contains repositories")


def install_policy(path: Path, backup_root: Path, group_id: int, *, enabled: bool) -> None:
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(backup_root, 0, 0)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"github-policy-before-{timestamp}.yaml"
    shutil.copy2(path, backup, follow_symlinks=False)
    os.chown(backup, 0, 0)
    backup.chmod(0o600)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    content = (
        (
            "# Enabled by an exact typed-operator backlog campaign approval.\n"
            "enabled: true\n"
            "safe_repositories:\n"
            + "".join(f"  - {repository}\n" for repository in SAFE_REPOSITORIES)
        )
        if enabled
        else (
            "# Disabled after the bounded backlog campaign reached a terminal state.\n"
            "enabled: false\n"
            "safe_repositories: []\n"
        )
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o640,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, group_id)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    if os.geteuid() != 0:
        raise PermissionError("GitHub policy reconciliation requires root")
    args = parse_args()
    api_uid = pwd.getpwnam("engineering-control").pw_uid
    group_id = grp.getgrnam("engineering-control").gr_gid
    validate_current_policy(args.policy, group_id)
    settings = Settings()
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    if args.audit_only:
        with Session(engine) as session:
            campaigns, reviewed, unapproved = audit_approval_actors(session)
            session.rollback()
        print(
            "GITHUB_BACKLOG_APPROVAL_ACTOR_AUDIT_PASS "
            f"campaigns={campaigns} approvals={reviewed} unapproved={unapproved}"
        )
        return 0
    if args.expire_only:
        with Session(engine) as session, session.begin():
            if expire_campaign_approvals(session, datetime.now(UTC)):
                policy = GitHubWritePolicyDocument.model_validate(
                    yaml.safe_load(args.policy.read_text(encoding="utf-8"))
                )
                if policy.enabled:
                    install_policy(args.policy, args.backup_root, group_id, enabled=False)
        print("GITHUB_BACKLOG_POLICY_EXPIRY_PASS")
        return 0
    request = read_request(args.request, api_uid)
    with Session(engine) as session, session.begin():
        campaign = validate_campaign(session, request)
        enable = request["action"] == "enable-existing-backlog"
        install_policy(args.policy, args.backup_root, group_id, enabled=enable)
        if enable:
            campaign.status = "APPROVED"
    args.request.unlink()
    print("GITHUB_BACKLOG_POLICY_RECONCILIATION_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
