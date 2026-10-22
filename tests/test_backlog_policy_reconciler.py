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
    canonical_operator_approval_actor,
    is_reauthenticated_backlog_operator,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = runpy.run_path(str(ROOT / "scripts/reconcile-backlog-github-policy.py"))
VALIDATE_CAMPAIGN = SCRIPT["validate_campaign"]
EXPIRE_CAMPAIGNS = SCRIPT["expire_campaign_approvals"]
READ_REQUEST = SCRIPT["read_request"]
AUDIT_ACTORS = SCRIPT["audit_approval_actors"]
OTHER_OWNER = SCRIPT["other_unexpired_owner"]
REJECT_REQUEST = SCRIPT["reject_request"]


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
                    "gntik-ai/gntik-ui",
                    "gntik-ai/falcone-serverless",
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
                    "gntik-ai/gntik-ui",
                    "gntik-ai/falcone-serverless",
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
    assert isinstance(BACKLOG_FIXED_OPERATOR_ACTORS, frozenset)
    assert BACKLOG_FIXED_OPERATOR_ACTORS == frozenset(
        {"dashboard:reauthenticated", "cli:engineering-ctl"}
    )


def test_typed_telegram_actor_accepts_any_canonical_id_after_ingress_authentication() -> None:
    assert is_reauthenticated_backlog_operator("telegram:43") is True


def test_authenticated_cli_subject_maps_to_stable_durable_operator() -> None:
    assert canonical_operator_approval_actor("engineering-ctl:hermes") == ("cli:engineering-ctl")
    assert canonical_operator_approval_actor("telegram:42") == "telegram:42"
    for invalid in (
        "engineering-ctl",
        "engineering-ctl:root",
        "cli:engineering-ctl",
        "dashboard:reauthenticated",
        "telegram:allowlisted",
    ):
        with pytest.raises(ValueError, match="authenticated operator identity"):
            canonical_operator_approval_actor(invalid)


@pytest.mark.parametrize(
    "actor",
    [
        "api:untrusted",
        "telegram:",
        "telegram:0",
        "telegram:042",
        "telegram:abc",
        "telegram:+42",
        "telegram:4_2",
        "telegram: 42",
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
    original_binding = owner.approval_binding
    request = {
        "action": "enable-existing-backlog",
        "backlog_campaign_id": str(campaign_id),
        "project": "falcone",
        "approval_binding": approval_binding or owner.approval_binding,
        "requested_by": requested_by,
    }
    with Session(engine) as session, session.begin():
        session.add(owner)
    with Session(engine) as session:
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        original_updated_at = stored.updated_at
    with Session(engine) as session:
        with pytest.raises(ValueError, match=message):
            VALIDATE_CAMPAIGN(session, request)
        assert not session.dirty
        assert not session.new
        assert not session.deleted
    with Session(engine) as session:
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        assert stored.status == "APPROVED_POLICY_PENDING"
        assert stored.approval_json == original_approval
        assert stored.approval_binding == original_binding
        assert stored.updated_at == original_updated_at
        assert session.query(GitHubBacklogCampaign).count() == 1


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
    valid_telegram = campaign(
        UUID("33333333-3333-4333-8333-333333333333"),
        project="falcone",
        status="APPROVED_INVENTORY",
        role="inventory",
        now=now,
        approved_by="telegram:42",
    )
    invalid_id = UUID("22222222-2222-4222-8222-222222222222")
    invalid = campaign(
        invalid_id,
        project="falcone",
        status="APPROVED_POLICY_PENDING",
        role="owner",
        now=now,
        approved_by="telegram:allowlisted",
    )
    with Session(engine) as session, session.begin():
        session.add_all([valid, valid_telegram])
    with Session(engine) as session:
        assert AUDIT_ACTORS(session) == (2, 2, 0)
    with Session(engine) as session, session.begin():
        session.add(invalid)
    with (
        Session(engine) as session,
        pytest.raises(ValueError, match="noncanonical operator actor") as error,
    ):
        AUDIT_ACTORS(session)
    assert f"campaign={invalid_id}" in str(error.value)
    assert "campaigns=" in str(error.value)
    assert "approvals=" in str(error.value)
    assert "unapproved=" in str(error.value)
    assert "telegram:allowlisted" not in str(error.value)


def test_durable_actor_audit_rejects_present_approval_without_actor(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'missing-actor-audit.sqlite'}")
    Base.metadata.create_all(engine)
    owner = campaign(
        UUID("11111111-1111-4111-8111-111111111111"),
        project="falcone",
        status="APPROVED_POLICY_PENDING",
        role="owner",
        now=datetime.now(UTC),
    )
    owner.approval_json["approval"].pop("approved_by")
    with Session(engine) as session, session.begin():
        session.add(owner)
    with Session(engine) as session, pytest.raises(ValueError, match="noncanonical operator actor"):
        AUDIT_ACTORS(session)


def test_durable_actor_audit_counts_unapproved_and_rejects_shape_disagreement(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'actor-shapes.sqlite'}")
    Base.metadata.create_all(engine)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    unapproved = campaign(
        campaign_id,
        project="falcone",
        status="AWAITING_APPROVAL",
        role="owner",
        now=datetime.now(UTC),
    )
    unapproved.approval_json = {}
    with Session(engine) as session, session.begin():
        session.add(unapproved)
    with Session(engine) as session:
        assert AUDIT_ACTORS(session) == (1, 0, 1)
    with Session(engine) as session, session.begin():
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        stored.status = "APPROVED_POLICY_PENDING"
        stored.approval_json = {
            "binding": {},
            "approval": {
                "approved_by": "telegram:42",
                "approved_at": datetime.now(UTC).isoformat(),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            },
            "approved_by": "cli:engineering-ctl",
        }
    with Session(engine) as session, pytest.raises(ValueError, match="shapes disagree"):
        AUDIT_ACTORS(session)


def test_durable_actor_audit_rejects_unknown_status_and_incomplete_approval(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fail-closed-audit.sqlite'}")
    Base.metadata.create_all(engine)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    unknown = campaign(
        campaign_id,
        project="falcone",
        status="FUTURE_STATE",
        role="owner",
        now=datetime.now(UTC),
    )
    unknown.approval_json = {}
    with Session(engine) as session, session.begin():
        session.add(unknown)
    with Session(engine) as session, pytest.raises(ValueError, match="lacks canonical"):
        AUDIT_ACTORS(session)

    with Session(engine) as session, session.begin():
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        stored.status = "APPROVED_POLICY_PENDING"
        stored.approval_json = {
            "binding": {},
            "approval": {
                "approved_by": "telegram:42",
                "approved_at": datetime.now(UTC).isoformat(),
            },
        }
    with Session(engine) as session, pytest.raises(ValueError, match="incomplete"):
        AUDIT_ACTORS(session)


def test_durable_actor_audit_aligns_nested_shapes_and_redacts_invalid_timestamps(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'nested-audit.sqlite'}")
    Base.metadata.create_all(engine)
    campaign_id = UUID("11111111-1111-4111-8111-111111111111")
    binding_only = campaign(
        campaign_id,
        project="falcone",
        status="AWAITING_APPROVAL",
        role="owner",
        now=datetime.now(UTC),
    )
    binding_only.approval_json = {"binding": binding_only.approval_json["binding"]}
    with Session(engine) as session, session.begin():
        session.add(binding_only)
    with Session(engine) as session:
        assert AUDIT_ACTORS(session) == (1, 0, 1)

    with Session(engine) as session, session.begin():
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        stored.status = "APPROVED_POLICY_PENDING"
        stored.approval_json = {
            "binding": stored.approval_json["binding"],
            "approved_by": "telegram:42",
            "approved_at": datetime.now(UTC).isoformat(),
            "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
        }
    with Session(engine) as session, pytest.raises(ValueError, match="malformed"):
        AUDIT_ACTORS(session)

    with Session(engine) as session, session.begin():
        stored = session.get(GitHubBacklogCampaign, campaign_id)
        assert stored is not None
        stored.approval_json = {
            "binding": stored.approval_json["binding"],
            "approval": {
                "approved_by": "telegram:42",
                "approved_at": "SENSITIVE_TIMESTAMP_PAYLOAD",
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            },
        }
    with (
        Session(engine) as session,
        pytest.raises(ValueError, match="incomplete") as error,
    ):
        AUDIT_ACTORS(session)
    assert "SENSITIVE_TIMESTAMP_PAYLOAD" not in str(error.value)
    assert error.value.__cause__ is None


def test_durable_actor_audit_accepts_terminal_evidence_and_one_phrase_only(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'terminal-audit.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    stopped_id = UUID("11111111-1111-4111-8111-111111111111")
    stopped = campaign(
        stopped_id,
        project="falcone",
        status="STOPPED",
        role="owner",
        now=now,
    )
    expired = campaign(
        UUID("22222222-2222-4222-8222-222222222222"),
        project="musematic",
        status="EXPIRED",
        role="inventory",
        now=now,
    )
    stopped_unapproved = campaign(
        UUID("33333333-3333-4333-8333-333333333333"),
        project="llmwiki",
        status="STOPPED",
        role="inventory",
        now=now,
    )
    stopped_unapproved.approval_json = {"binding": stopped_unapproved.approval_json["binding"]}
    with Session(engine) as session, session.begin():
        session.add_all([stopped, expired, stopped_unapproved])
    with Session(engine) as session:
        assert AUDIT_ACTORS(session) == (3, 2, 1)

    with Session(engine) as session, session.begin():
        stored = session.get(GitHubBacklogCampaign, stopped_id)
        assert stored is not None
        evidence = dict(stored.approval_json)
        approval = dict(evidence["approval"])
        approval["typed_confirmation"] = "APPROVE_STAGING_ISSUE_DELIVERY"
        evidence["approval"] = approval
        stored.approval_json = evidence
    with Session(engine) as session, pytest.raises(ValueError, match="incomplete"):
        AUDIT_ACTORS(session)


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


def test_install_policy_is_0640_under_the_units_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NEW-25: the reconciler unit runs with UMask=0077, which turned os.open's 0o640 into
    # 0o600. Its own validate_current_policy and the GitHub worker then rejected the policy
    # it had just installed, failing every later run and blocking PR writes.
    import stat

    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    policy = tmp_path / "policy.yaml"
    policy.write_text("enabled: false\nsafe_repositories: []\n", encoding="utf-8")

    previous = os.umask(0o077)
    try:
        SCRIPT["install_policy"](policy, tmp_path / "backups", os.getgid(), enabled=True)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(policy.stat().st_mode) == 0o640
    assert "enabled: true" in policy.read_text(encoding="utf-8")


def test_disable_keeps_the_shared_policy_for_another_unexpired_owner(tmp_path: Path) -> None:
    # NEW-94 (2026-09-28): cancelling the stale falcone #46 loop (campaign e0105017) sent a
    # disable request that turned off the shared policy owned by the running falcone #1025
    # campaign; its publication then waited on POLICY_DISABLED for hours.
    engine = create_engine(f"sqlite:///{tmp_path / 'other-owner.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    ending = UUID("11111111-1111-4111-8111-111111111111")
    owner = UUID("22222222-2222-4222-8222-222222222222")
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                campaign(ending, project="falcone", status="CANCELLED", role="owner", now=now),
                campaign(owner, project="falcone", status="RUNNING", role="owner", now=now),
            ]
        )
    with Session(engine) as session:
        assert OTHER_OWNER(session, ending, now) == owner
        # The owner itself ending never counts itself.
        assert OTHER_OWNER(session, owner, now) is None


@pytest.mark.parametrize(
    ("status", "role", "age"),
    [
        ("RUNNING", "owner", timedelta(days=2)),  # expired approval
        ("APPROVED_INVENTORY", "inventory", timedelta(0)),  # inventory-only, not an owner
        ("CANCELLED", "owner", timedelta(0)),  # terminal, not an owner
        ("PAUSED", "inventory", timedelta(0)),
    ],
)
def test_disable_proceeds_without_another_unexpired_owner(
    tmp_path: Path, status: str, role: str, age: timedelta
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'no-other-owner.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    ending = UUID("11111111-1111-4111-8111-111111111111")
    other = UUID("22222222-2222-4222-8222-222222222222")
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                campaign(ending, project="falcone", status="STOPPED", role="owner", now=now),
                campaign(other, project="musematic", status=status, role=role, now=now - age),
            ]
        )
    with Session(engine) as session:
        assert OTHER_OWNER(session, ending, now) is None


def test_a_tampered_or_incomplete_owner_never_keeps_the_policy(tmp_path: Path) -> None:
    # NEW-94 review: only fully verified approval evidence keeps the shared policy enabled;
    # a forged future expires_at (binding hash no longer matches) or missing approval
    # evidence counts as no owner, both for a disable request and for the expiry pass.
    now = datetime.now(UTC)
    ending = UUID("11111111-1111-4111-8111-111111111111")
    other = UUID("22222222-2222-4222-8222-222222222222")
    for name, mutate in (
        (
            "forged-expiry",
            lambda e: e["binding"].update(expires_at=(now + timedelta(days=9)).isoformat()),
        ),
        ("no-approval-actor", lambda e: e["approval"].pop("approved_by")),
        ("wrong-phrase", lambda e: e["approval"].update(typed_confirmation="yes")),
    ):
        engine = create_engine(f"sqlite:///{tmp_path / (name + '.sqlite')}")
        Base.metadata.create_all(engine)
        owner = campaign(
            other, project="falcone", status="RUNNING", role="owner", now=now - timedelta(days=2)
        )
        evidence = json.loads(json.dumps(owner.approval_json))
        mutate(evidence)
        owner.approval_json = evidence
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    campaign(ending, project="falcone", status="CANCELLED", role="owner", now=now),
                    owner,
                ]
            )
        with Session(engine) as session:
            assert OTHER_OWNER(session, ending, now) is None, name
        with Session(engine) as session, session.begin():
            assert EXPIRE_CAMPAIGNS(session, now) is True, name


def test_copied_or_malformed_owner_evidence_never_keeps_the_policy(tmp_path: Path) -> None:
    # NEW-94 review round 2: evidence copied from another campaign (valid hash, wrong
    # campaign id) is not an owner, and malformed evidence expires instead of crashing the
    # fail-safe expiry pass.
    now = datetime.now(UTC)
    donor = UUID("33333333-3333-4333-8333-333333333333")
    copier = UUID("22222222-2222-4222-8222-222222222222")
    ending = UUID("11111111-1111-4111-8111-111111111111")
    engine = create_engine(f"sqlite:///{tmp_path / 'copied.sqlite'}")
    Base.metadata.create_all(engine)
    source = campaign(donor, project="falcone", status="COMPLETE", role="owner", now=now)
    copied = campaign(copier, project="falcone", status="RUNNING", role="owner", now=now)
    copied.approval_json = json.loads(json.dumps(source.approval_json))
    copied.approval_binding = source.approval_binding
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                source,
                copied,
                campaign(ending, project="falcone", status="CANCELLED", role="owner", now=now),
            ]
        )
    with Session(engine) as session:
        assert OTHER_OWNER(session, ending, now) is None
    with Session(engine) as session, session.begin():
        assert EXPIRE_CAMPAIGNS(session, now) is True

    engine = create_engine(f"sqlite:///{tmp_path / 'malformed.sqlite'}")
    Base.metadata.create_all(engine)
    broken = campaign(copier, project="falcone", status="RUNNING", role="owner", now=now)
    broken.approval_json = ["not", "an", "object"]  # type: ignore[assignment]
    with Session(engine) as session, session.begin():
        session.add(broken)
    with Session(engine) as session, session.begin():
        assert EXPIRE_CAMPAIGNS(session, now) is True
    with Session(engine) as session:
        assert session.get(GitHubBacklogCampaign, copier).status == "EXPIRED"


@pytest.mark.parametrize("defect", ["junk-approved-at", "moved-heads", "changed-plan"])
def test_owner_must_pass_the_workers_approval_check(tmp_path: Path, defect: str) -> None:
    # NEW-94 review round 3: the reconciler uses the workers' own evidence check
    # (verified_backlog_approval), so an unparseable approved_at or a binding that no longer
    # matches the imported heads / plan is not an owner, and the row is expired so it can
    # neither keep the policy enabled nor block a replacement approval.
    now = datetime.now(UTC)
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    ending = UUID("11111111-1111-4111-8111-111111111111")
    engine = create_engine(f"sqlite:///{tmp_path / (defect + '.sqlite')}")
    Base.metadata.create_all(engine)
    owner = campaign(owner_id, project="falcone", status="RUNNING", role="owner", now=now)
    if defect == "junk-approved-at":
        evidence = json.loads(json.dumps(owner.approval_json))
        evidence["approval"]["approved_at"] = "junk"
        owner.approval_json = evidence
    elif defect == "moved-heads":
        owner.source_head_at_import = "d" * 40
    else:
        owner.plan_hash = "e" * 64
    with Session(engine) as session, session.begin():
        session.add_all(
            [owner, campaign(ending, project="falcone", status="CANCELLED", role="owner", now=now)]
        )
    with Session(engine) as session:
        assert OTHER_OWNER(session, ending, now) is None
    with Session(engine) as session, session.begin():
        assert EXPIRE_CAMPAIGNS(session, now) is True
    with Session(engine) as session:
        assert session.get(GitHubBacklogCampaign, owner_id).status == "EXPIRED"


def test_a_verified_paused_owner_still_keeps_the_policy(tmp_path: Path) -> None:
    # A loop paused for an operator retry (falcone #1025 on 2026-09-28) must keep owning the
    # policy, or another campaign ending would disable it again (the original NEW-94).
    now = datetime.now(UTC)
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    ending = UUID("11111111-1111-4111-8111-111111111111")
    engine = create_engine(f"sqlite:///{tmp_path / 'paused.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                campaign(owner_id, project="falcone", status="PAUSED", role="owner", now=now),
                campaign(ending, project="falcone", status="CANCELLED", role="owner", now=now),
            ]
        )
    with Session(engine) as session:
        assert OTHER_OWNER(session, ending, now) == owner_id
    with Session(engine) as session, session.begin():
        assert EXPIRE_CAMPAIGNS(session, now) is False


def test_a_rejected_request_is_moved_aside_so_it_cannot_block_the_queue(tmp_path: Path) -> None:
    # NEW-94 review round 5: writers never overwrite a pending request, so a request the
    # reconciler rejects must leave activate.json, or every later request would fail.
    request = tmp_path / "activate.json"
    body = b'{"action":"enable-existing-backlog"}\n'
    request.write_bytes(body)
    REJECT_REQUEST(request, body)
    assert not request.exists()
    [rejected] = list(tmp_path.glob("rejected-*.json"))
    assert rejected.read_bytes() == body
    # A newer request that replaced the rejected one is left alone.
    request.write_bytes(b'{"action":"disable-existing-backlog"}\n')
    REJECT_REQUEST(request, body)
    assert request.exists()
