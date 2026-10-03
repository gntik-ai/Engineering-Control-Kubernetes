"""Recovery and enrollment of Run temporal binding from verified Temporal lineage.

Only persists data obtained by an approved Temporal client via prove_execution_start.
No caller-supplied proof objects are accepted or deserialized.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.client import Client

from engineering_control.db.models import Campaign, Run
from engineering_control.db.run_binding import bind_run_execution
from engineering_control.db.runtime_binding import _current_identity
from engineering_control.db.start_proof import ExecutionStartProof, prove_execution_start
from engineering_control.operations.connection_budget import joined_diagnostic_thread

_UNVERIFIABLE_MSG = "execution start proof is unverifiable"
_NO_IDENTITY_MSG = "no verified activity run binding in context"
_IDENTITY_MISMATCH_MSG = "activity execution does not match verified binding context"
_AMBIGUOUS_MSG = "multiple runs bound to this workflow_id"
_FIRST_MISMATCH_MSG = "proof first_run_id does not match verified binding context"
_CAMPAIGN_MISSING_MSG = "campaign does not match run"
_CAMPAIGN_AMBIGUOUS_MSG = "campaign has more than one audit-campaign run"
_METADATA_CONFLICT_MSG = "campaign metadata already bound to a different execution"
_METADATA_TYPE_MSG = "campaign metadata_json is not a dict"

_WORKFLOW_TYPE_MAP = {
    "audit-campaign": "AuditCampaignWorkflow",
    "multi-project-audit": "MultiProjectAuditWorkflow",
    "finding-verification": "FindingVerificationWorkflow",
    "issue-delivery": "IssueDeliveryWorkflow",
    "delivery-reconciliation": "DeliveryReconciliationWorkflow",
    "preview-deployment": "PreviewDeploymentWorkflow",
    "staging-deployment": "StagingDeploymentWorkflow",
    "runtime-verification": "RuntimeVerificationWorkflow",
    "recovery": "RecoveryWorkflow",
    # Main (NEW-87 staging GitOps): the backlog loop pre-registers this child Run.
    "staging-gitops": "StagingGitOpsDeliveryWorkflow",
}


@contextmanager
def _locked_transaction(session: Session, lock_ms: int, statement_ms: int) -> Iterator[None]:
    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        if lock_ms != 3000 or statement_ms not in {3000, 5000}:
            raise ValueError("unsupported run enrollment timeout")
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        session.execute(
            text(
                "SET LOCAL statement_timeout = '3s'"
                if statement_ms == 3000
                else "SET LOCAL statement_timeout = '5s'"
            )
        )
    elif dialect != "sqlite":
        raise ValueError("unsupported database dialect for run enrollment")
    yield


def _persist_proof(sessions: sessionmaker[Session], proof: ExecutionStartProof) -> bool:
    if proof.application_run_id is None:
        raise ValueError(_UNVERIFIABLE_MSG)
    changed = False
    with sessions.begin() as session:
        with _locked_transaction(session, 3000, 5000):
            row = session.execute(
                select(Run).where(Run.id == proof.application_run_id)
            ).scalar_one_or_none()
            if row is None or row.workflow_id != proof.workflow_id:
                raise ValueError(_UNVERIFIABLE_MSG)
            run_type = row.run_type
            expected_type = _WORKFLOW_TYPE_MAP.get(run_type)
            if expected_type is None or expected_type != proof.workflow_type:
                raise ValueError(_UNVERIFIABLE_MSG)

            campaign_id = row.campaign_id
            campaign = None
            if run_type == "audit-campaign":
                if campaign_id is None:
                    raise ValueError(_UNVERIFIABLE_MSG)
                campaign = session.execute(
                    select(Campaign)
                    .where(Campaign.id == campaign_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one_or_none()
                if campaign is None:
                    raise ValueError(_CAMPAIGN_MISSING_MSG)
                row = session.execute(
                    select(Run)
                    .where(Run.id == proof.application_run_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.workflow_id != proof.workflow_id
                    or row.run_type != run_type
                    or row.campaign_id != campaign_id
                ):
                    raise ValueError(_UNVERIFIABLE_MSG)
                if campaign.project != row.project:
                    raise ValueError(_CAMPAIGN_MISSING_MSG)
                identities = (
                    session.execute(
                        select(Run.id)
                        .where(Run.campaign_id == campaign_id, Run.run_type == "audit-campaign")
                        .limit(2)
                    )
                    .scalars()
                    .all()
                )
                if len(identities) != 1 or identities[0] != proof.application_run_id:
                    raise ValueError(_CAMPAIGN_AMBIGUOUS_MSG)
            else:
                row = session.execute(
                    select(Run)
                    .where(Run.id == proof.application_run_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.workflow_id != proof.workflow_id
                    or row.run_type != run_type
                    or row.campaign_id != campaign_id
                ):
                    raise ValueError(_UNVERIFIABLE_MSG)

            if session.new or session.dirty or session.deleted:
                raise ValueError(_UNVERIFIABLE_MSG)

            bound = bind_run_execution(
                session, proof.application_run_id, proof.workflow_id, proof.first_run_id
            )
            changed = changed or bound

            if run_type == "audit-campaign":
                if campaign is None:
                    raise ValueError(_CAMPAIGN_MISSING_MSG)
                metadata = campaign.metadata_json
                if type(metadata) is not dict:
                    raise ValueError(_METADATA_TYPE_MSG)
                existing_binding = metadata.get("reconciliation_binding")
                new_binding = {
                    "version": 1,
                    "workflow_id": proof.workflow_id,
                    "first_execution_run_id": proof.first_run_id,
                    "application_run_id": str(proof.application_run_id),
                }
                if existing_binding is None:
                    updated = dict(metadata)
                    updated["reconciliation_binding"] = new_binding
                    campaign.metadata_json = updated
                    changed = True
                elif existing_binding != new_binding:
                    raise ValueError(_METADATA_CONFLICT_MSG)
    return changed


async def recover_recorded_run(
    sessions: sessionmaker[Session],
    client: Client,
    namespace: str,
    workflow_id: str,
    current_run_id: str,
    application_run_id: UUID,
    expected_first_run_id: str | None = None,
) -> bool:
    if type(application_run_id) is not UUID or application_run_id.int == 0:
        raise ValueError(_UNVERIFIABLE_MSG)
    proof = await prove_execution_start(client, namespace, workflow_id, current_run_id)
    if proof.application_run_id is None or proof.application_run_id != application_run_id:
        raise ValueError(_UNVERIFIABLE_MSG)
    if expected_first_run_id is not None and proof.first_run_id != expected_first_run_id:
        raise ValueError(_FIRST_MISMATCH_MSG)
    return await joined_diagnostic_thread(_persist_proof, sessions, proof)


def _select_candidates(
    sessions: sessionmaker[Session], workflow_id: str
) -> list[tuple[UUID, str | None]]:
    with sessions.begin() as session:
        with _locked_transaction(session, 3000, 3000):
            bind = session.get_bind()
            if bind.dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            rows = session.execute(
                select(Run.id, Run.temporal_first_execution_run_id)
                .where(Run.workflow_id == workflow_id)
                .limit(2)
            ).all()
    return [(r[0], r[1]) for r in rows]


async def enroll_activity_run(sessions: sessionmaker[Session]) -> str:
    context = _current_identity.get(None)
    if context is None:
        raise ValueError(_NO_IDENTITY_MSG)

    info = activity.info()
    if info.workflow_id != context.workflow_id or info.workflow_run_id != context.current_run_id:
        raise ValueError(_IDENTITY_MISMATCH_MSG)

    client = activity.client()
    namespace = info.namespace
    if namespace != client.namespace:
        raise ValueError(_IDENTITY_MISMATCH_MSG)

    rows = await asyncio.to_thread(_select_candidates, sessions, context.workflow_id)

    if len(rows) == 0:
        return "NEW_ROW"
    if len(rows) > 1:
        raise ValueError(_AMBIGUOUS_MSG)

    run_id, first_run_id = rows[0]
    if first_run_id is not None:
        if first_run_id != context.first_run_id:
            raise ValueError(_UNVERIFIABLE_MSG)
        return "ALREADY_BOUND"

    await recover_recorded_run(
        sessions,
        client,
        namespace,
        context.workflow_id,
        context.current_run_id,
        run_id,
        expected_first_run_id=context.first_run_id,
    )
    return "ENROLLED"
