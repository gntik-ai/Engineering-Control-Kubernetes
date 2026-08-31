from __future__ import annotations

import json
import os
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from engineering_control.db.base import Base
from engineering_control.db.models import GitHubBacklogCampaign
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BACKLOG_FIXED_OPERATOR_ACTORS,
    approval_binding,
    is_reauthenticated_backlog_operator,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = runpy.run_path(str(ROOT / "scripts/reconcile-backlog-github-policy.py"))
VALIDATE_CAMPAIGN = SCRIPT["validate_campaign"]
EXPIRE_CAMPAIGNS = SCRIPT["expire_campaign_approvals"]
READ_REQUEST = SCRIPT["read_request"]
AUDIT_ACTORS = SCRIPT["audit_approval_actors"]


def campaign(
    campaign_id: UUID,
    *,
    project: str,
    status: str,
    role: str,
    now: datetime,
    approved_by: str = "telegram:42",
) -> GitHubBacklogCampaign:
    pairs = {
        "falcone": ("gntik-ai/falcone", "gntik-ai/falcone-charts"),
        "musematic": ("gntik-ai/musematic", "gntik-ai/musematic-deploy"),
        "llmwiki": ("gntik-ai/llmwiki", "gntik-ai/llmwiki"),
    }
    source, deployment = pairs[project]
    delivery_mode = "source-pr" if project == "llmwiki" else "full-delivery"
    binding = approval_binding(
        campaign_id=str(campaign_id),
        project=project,  # type: ignore[arg-type]
        import_filter="all",
        source_head="a" * 40,
        deployment_head="b" * 40,
        dependency_plan_hash="c" * 64,
        maximum_risk="R2",
        maximum_items=1,
        delivery_mode=delivery_mode,  # type: ignore[arg-type]
        include_enhancements=False,
        now=now,
    )
    return GitHubBacklogCampaign(
        id=campaign_id,
        project=project,
        source_repository=source,
        deployment_repository=deployment,
        imported_at=now,
        issue_query="is:issue is:open",
        import_filter="all",
        source_default_branch="master" if project == "llmwiki" else "main",
        deployment_default_branch="master" if project == "llmwiki" else "main",
        source_head_at_import="a" * 40,
        deployment_head_at_import="b" * 40,
        issue_count=1,
        eligible_count=1,
        deferred_count=0,
        duplicate_count=0,
        stale_count=0,
        already_fixed_count=0,
        existing_pr_count=0,
        blocked_count=0,
        dependency_graph_hash="c" * 64,
        plan_hash="c" * 64,
        approval_binding=binding.sha256(),
        approval_json={
            "binding": binding.model_dump(mode="json"),
            "policy_role": role,
            "approval": {
                "approved_by": approved_by,
                "approved_at": now.isoformat(),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            },
        },
        maximum_risk="R2",
        maximum_items=1,
        maximum_concurrency=1,
        delivery_mode=delivery_mode,
        include_enhancements=False,
        status=status,
        workflow_id=f"backlog-import-{campaign_id}",
    )


@pytest.mark.parametrize(
    "approved_by",
    ["telegram:42", "dashboard:reauthenticated", "cli:engineering-ctl"],
)
def test_root_reconciler_accepts_every_typed_reauthenticated_approval_actor(
    tmp_path: Path,
    approved_by: str,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'approval-actors.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    owner = campaign(
        campaign_id,
        project="falcone",
        status="APPROVED_POLICY_PENDING",
        role="owner",
        now=now,
        approved_by=approved_by,
    )
    request = {
        "action": "enable-existing-backlog",
        "backlog_campaign_id": str(campaign_id),
        "project": "falcone",
        "approval_binding": owner.approval_binding,
        "requested_by": approved_by,
    }
    with Session(engine) as session, session.begin():
        session.add(owner)
    with Session(engine) as session:
        assert VALIDATE_CAMPAIGN(session, request).id == campaign_id


@pytest.mark.parametrize(
    "requested_by",
    ["telegram:42", "dashboard:reauthenticated", "cli:engineering-ctl"],
)
def test_policy_request_accepts_every_typed_reauthenticated_operator(
    tmp_path: Path,
    requested_by: str,
) -> None:
    request_path = tmp_path / "activate.json"
    request_path.write_text(
        json.dumps(
            {
                "action": "enable-existing-backlog",
                "backlog_campaign_id": "11111111-1111-4111-8111-111111111111",
                "project": "falcone",
                "approval_binding": "a" * 64,
                "safe_repositories": [
                    "gntik-ai/falcone",
                    "gntik-ai/falcone-charts",
                    "gntik-ai/musematic",
                    "gntik-ai/musematic-deploy",
                    "gntik-ai/llmwiki",
                ],
                "requested_by": requested_by,
                "requested_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    request_path.chmod(0o600)

    assert READ_REQUEST(request_path, os.getuid())["requested_by"] == requested_by


def test_policy_request_rejects_an_untyped_actor(tmp_path: Path) -> None:
    request_path = tmp_path / "activate.json"
    request_path.write_text(
        json.dumps(
            {
                "action": "enable-existing-backlog",
                "backlog_campaign_id": "11111111-1111-4111-8111-111111111111",
                "project": "falcone",
                "approval_binding": "a" * 64,
                "safe_repositories": [
                    "gntik-ai/falcone",
                    "gntik-ai/falcone-charts",
                    "gntik-ai/musematic",
                    "gntik-ai/musematic-deploy",
                    "gntik-ai/llmwiki",
                ],
                "requested_by": "api:untrusted",
                "requested_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    request_path.chmod(0o600)

    with pytest.raises(ValueError, match="typed reauthenticated operator"):
        READ_REQUEST(request_path, os.getuid())


def test_policy_request_rejects_wrong_owner_mode_and_symlink(tmp_path: Path) -> None:
    request_path = tmp_path / "activate.json"
    request_path.write_text("{}", encoding="utf-8")
    request_path.chmod(0o600)
    with pytest.raises(ValueError, match="ownership or mode"):
        READ_REQUEST(request_path, os.getuid() + 1)
    for unsafe_mode in (0o644, 0o660):
        request_path.chmod(unsafe_mode)
        with pytest.raises(ValueError, match="ownership or mode"):
            READ_REQUEST(request_path, os.getuid())
    request_path.chmod(0o600)
    symlink_path = tmp_path / "activate-link.json"
    symlink_path.symlink_to(request_path)
    with pytest.raises(ValueError, match="regular non-symlink"):
        READ_REQUEST(symlink_path, os.getuid())


@pytest.mark.parametrize(
    "actor",
    ["telegram:42", "dashboard:reauthenticated", "cli:engineering-ctl"],
)
def test_shared_backlog_operator_policy_accepts_exact_identities(actor: str) -> None:
    assert is_reauthenticated_backlog_operator(actor) is True


def test_shared_backlog_operator_policy_fixed_set_is_fail_closed() -> None:
    assert BACKLOG_FIXED_OPERATOR_ACTORS == frozenset(
        {"dashboard:reauthenticated", "cli:engineering-ctl"}
    )


@pytest.mark.parametrize(
    "actor",
    [
        "api:untrusted",
        "telegram:",
        "telegram:0",
        "telegram:042",
        "telegram:abc",
        "telegram:12345678901234567890",
        " telegram:42",
        "telegram:42 ",
        "telegram:42\n",
        "Telegram:42",
        "TELEGRAM:42",
        "telegram:\u0664\u0662",
        "dashboard:",
        "dashboard:session",
        "dashboard:reauthenticated ",
        "cli:",
        "cli:other",
        "cli:engineering-ctl ",
    ],
)
def test_shared_backlog_operator_policy_rejects_near_matches(actor: str) -> None:
    assert is_reauthenticated_backlog_operator(actor) is False


def test_root_reconciler_accepts_exact_stopped_owner_and_rejects_malformed_nested(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciler.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    owner = campaign(
        campaign_id,
        project="falcone",
        status="STOPPED",
        role="owner",
        now=now,
    )
    request = {
        "action": "disable-existing-backlog",
        "backlog_campaign_id": str(campaign_id),
        "project": "falcone",
        "approval_binding": owner.approval_binding,
        "requested_by": "telegram:42",
    }
    validate = VALIDATE_CAMPAIGN
    with Session(engine) as session, session.begin():
        session.add(owner)
    with Session(engine) as session:
        assert validate(session, request).id == campaign_id
    with Session(engine) as session, session.begin():
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        stored.approval_json = {"binding": stored.approval_json["binding"]}
    with Session(engine) as session, pytest.raises(ValueError, match="nested approval"):
        validate(session, request)


@pytest.mark.parametrize(
    ("requested_by", "approval_binding", "message"),
    [
        ("cli:engineering-ctl", None, "actor changed"),
        ("api:untrusted", None, "typed reauthenticated operator"),
        ("telegram:42", "f" * 64, "no current approved campaign"),
    ],
)
def test_root_reconciler_rejects_actor_or_binding_mismatch_without_mutation(
    tmp_path: Path,
    requested_by: str,
    approval_binding: str | None,
    message: str,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'mismatch.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    owner = campaign(
        campaign_id,
        project="falcone",
        status="APPROVED_POLICY_PENDING",
        role="owner",
        now=now,
    )
    original_approval = json.loads(json.dumps(owner.approval_json))
    request = {
        "action": "enable-existing-backlog",
        "backlog_campaign_id": str(campaign_id),
        "project": "falcone",
        "approval_binding": approval_binding or owner.approval_binding,
        "requested_by": requested_by,
    }
    with Session(engine) as session, session.begin():
        session.add(owner)
    with Session(engine) as session, pytest.raises(ValueError, match=message):
        VALIDATE_CAMPAIGN(session, request)
    with Session(engine) as session:
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        assert stored.status == "APPROVED_POLICY_PENDING"
        assert stored.approval_json == original_approval


def test_durable_actor_audit_accepts_canonical_and_rejects_legacy_shape(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'actor-audit.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    valid = campaign(
        UUID("11111111-1111-4111-8111-111111111111"),
        project="falcone",
        status="APPROVED_POLICY_PENDING",
        role="owner",
        now=now,
        approved_by="cli:engineering-ctl",
    )
    invalid = campaign(
        UUID("22222222-2222-4222-8222-222222222222"),
        project="musematic",
        status="APPROVED_INVENTORY",
        role="inventory",
        now=now,
        approved_by="telegram:allowlisted",
    )
    with Session(engine) as session, session.begin():
        session.add(valid)
    with Session(engine) as session:
        assert AUDIT_ACTORS(session) == 1
    with Session(engine) as session, session.begin():
        session.add(invalid)
    with (
        Session(engine) as session,
        pytest.raises(ValueError, match="noncanonical operator actor") as error,
    ):
        AUDIT_ACTORS(session)
    assert str(error.value) == "durable backlog approval contains a noncanonical operator actor"


def test_expiry_disables_and_expires_running_inventory_when_owner_is_expired(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expiry.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    owner_id = UUID("11111111-1111-4111-8111-111111111111")
    inventory_id = UUID("22222222-2222-4222-8222-222222222222")
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                campaign(
                    owner_id,
                    project="falcone",
                    status="APPROVED",
                    role="owner",
                    now=now - timedelta(days=2),
                ),
                campaign(
                    inventory_id,
                    project="musematic",
                    status="RUNNING",
                    role="inventory",
                    now=now,
                ),
            ]
        )
    expire = EXPIRE_CAMPAIGNS
    with Session(engine) as session, session.begin():
        assert expire(session, now) is True
    with Session(engine) as session:
        assert session.get(GitHubBacklogCampaign, owner_id).status == "EXPIRED"
        assert session.get(GitHubBacklogCampaign, inventory_id).status == "EXPIRED"


def test_expiry_keeps_policy_for_current_owner_and_expires_inventory(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expiry-owner.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    owner_id = UUID("11111111-1111-4111-8111-111111111111")
    inventory_id = UUID("22222222-2222-4222-8222-222222222222")
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                campaign(
                    owner_id,
                    project="falcone",
                    status="APPROVED",
                    role="owner",
                    now=now,
                ),
                campaign(
                    inventory_id,
                    project="musematic",
                    status="APPROVED_INVENTORY",
                    role="inventory",
                    now=now - timedelta(days=2),
                ),
            ]
        )
    expire = EXPIRE_CAMPAIGNS
    with Session(engine) as session, session.begin():
        assert expire(session, now) is False
    with Session(engine) as session:
        assert session.get(GitHubBacklogCampaign, owner_id).status == "APPROVED"
        assert session.get(GitHubBacklogCampaign, inventory_id).status == "EXPIRED"


def test_expiry_keeps_policy_while_current_owner_activation_is_pending(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expiry-pending-owner.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    owner_id = UUID("11111111-1111-4111-8111-111111111111")
    with Session(engine) as session, session.begin():
        session.add(
            campaign(
                owner_id,
                project="falcone",
                status="APPROVED_POLICY_PENDING",
                role="owner",
                now=now,
            )
        )
    with Session(engine) as session, session.begin():
        assert EXPIRE_CAMPAIGNS(session, now) is False
    with Session(engine) as session:
        assert session.get(GitHubBacklogCampaign, owner_id).status == "APPROVED_POLICY_PENDING"


def test_disable_accepts_expired_terminal_owner_for_fail_safe_policy_removal(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expired-disable.sqlite'}")
    Base.metadata.create_all(engine)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    owner = campaign(
        campaign_id,
        project="falcone",
        status="STOPPED",
        role="owner",
        now=datetime.now(UTC) - timedelta(days=2),
    )
    request = {
        "action": "disable-existing-backlog",
        "backlog_campaign_id": str(campaign_id),
        "project": "falcone",
        "approval_binding": owner.approval_binding,
        "requested_by": "telegram:42",
    }
    with Session(engine) as session, session.begin():
        session.add(owner)
    with Session(engine) as session:
        assert VALIDATE_CAMPAIGN(session, request).id == campaign_id


def test_owner_expiry_also_expires_orphaned_inventory_approvals(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'orphaned-inventory.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    owner_id = UUID("11111111-1111-4111-8111-111111111111")
    inventory_id = UUID("22222222-2222-4222-8222-222222222222")
    with Session(engine) as session, session.begin():
        session.add(
            campaign(
                owner_id,
                project="falcone",
                status="APPROVED",
                role="owner",
                now=now - timedelta(days=2),
            )
        )
        session.add(
            campaign(
                inventory_id,
                project="musematic",
                status="APPROVED_INVENTORY",
                role="inventory",
                now=now,
            )
        )
    with Session(engine) as session, session.begin():
        assert EXPIRE_CAMPAIGNS(session, now) is True
    with Session(engine) as session:
        assert session.get(GitHubBacklogCampaign, owner_id).status == "EXPIRED"
        assert session.get(GitHubBacklogCampaign, inventory_id).status == "EXPIRED"
