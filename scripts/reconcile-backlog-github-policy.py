#!/usr/bin/env python3
"""Apply one Telegram-approved backlog policy request as a root-only reconciliation."""

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
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.policy.github_write import GitHubWritePolicyDocument

SAFE_REPOSITORIES = (
    "gntik-ai/falcone",
    "gntik-ai/falcone-charts",
    "gntik-ai/musematic",
    "gntik-ai/musematic-deploy",
    "gntik-ai/llmwiki",
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
    parser.add_argument("--expire-only", action="store_true")
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
    if not str(value["requested_by"]).startswith("telegram:"):
        raise ValueError("GitHub policy request is not Telegram-approved")
    UUID(str(value["backlog_campaign_id"]))
    if len(str(value["approval_binding"])) != 64:
        raise ValueError("GitHub policy approval binding is invalid")
    datetime.fromisoformat(str(value["requested_at"]))
    return value


def validate_campaign(session: Session, request: dict[str, Any]) -> GitHubBacklogCampaign:
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
    raw_binding = campaign.approval_json.get("binding")
    binding = raw_binding if isinstance(raw_binding, dict) else campaign.approval_json
    raw_approval = campaign.approval_json.get("approval")
    approval = raw_approval if isinstance(raw_approval, dict) else campaign.approval_json
    if campaign.approval_json.get("policy_role") not in {None, "owner"}:
        raise ValueError("GitHub campaign approval evidence is incomplete")
    if canonical_sha256(binding) != request["approval_binding"]:
        raise ValueError("GitHub campaign approval binding changed")
    if approval.get("approved_by") != request["requested_by"]:
        raise ValueError("GitHub policy request actor changed")
    expires_at = datetime.fromisoformat(str(binding["expires_at"]))
    if expires_at <= datetime.now(UTC):
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
            "# Enabled by an exact Telegram backlog campaign approval.\n"
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
    if args.expire_only:
        with Session(engine) as session, session.begin():
            active = session.scalars(
                select(GitHubBacklogCampaign).where(
                    GitHubBacklogCampaign.status.in_({"APPROVED", "RUNNING", "PAUSED"})
                )
            ).all()
            unexpired = []
            for campaign in active:
                binding = campaign.approval_json.get("binding")
                try:
                    expires_at = (
                        datetime.fromisoformat(str(binding.get("expires_at")))
                        if isinstance(binding, dict)
                        else None
                    )
                except (TypeError, ValueError):
                    expires_at = None
                if expires_at is not None and expires_at > datetime.now(UTC):
                    unexpired.append(campaign)
                else:
                    campaign.status = "EXPIRED"
            if not unexpired:
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
