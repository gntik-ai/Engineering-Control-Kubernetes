"""Read-only CAS snapshotting and conservative reconciliation writes for Run/Campaign."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import Campaign, Run
from engineering_control.operations.reconciliation import safe_execution_binding, utc_text
from engineering_control.operations.run_reconciliation import (
    TERMINAL_OUTCOMES,
    RunDecision,
    decide_run_reconciliation,
    run_snapshot_token,
)

_AUDIT_CAMPAIGN = "audit-campaign"


class RunProjectionStore:
    def __init__(self, engine: Engine, sessions: sessionmaker[Session]) -> None:
        self._engine = engine
        self._sessions = sessions

    def _budget(self, session: Session, write: bool = False) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            return
        if dialect != "postgresql":
            raise ValueError(f"unsupported dialect: {dialect}")
        if write:
            session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        session.execute(text("SET LOCAL lock_timeout = '1s'"))
        if not write:
            session.execute(text("SET TRANSACTION READ ONLY"))

    def _candidate(self, session: Session, row: Run, campaign: Campaign | None) -> dict[str, Any]:
        run_count = len(
            session.execute(select(Run.id).where(Run.workflow_id == row.workflow_id).limit(2))
            .scalars()
            .all()
        )

        campaign_run_count = 0
        if campaign is not None and row.campaign_id is not None and campaign.project == row.project:
            campaign_run_count = len(
                session.execute(
                    select(Run.id)
                    .where(Run.campaign_id == row.campaign_id, Run.run_type == _AUDIT_CAMPAIGN)
                    .limit(2)
                )
                .scalars()
                .all()
            )

        campaign_status: str | None = None
        campaign_updated_at: str | None = None
        campaign_metadata_sha256: str | None = None
        binding_present = False
        binding_conflict = False

        if campaign is not None:
            valid_metadata = type(campaign.metadata_json) is dict
            metadata = campaign.metadata_json if valid_metadata else {}
            binding_conflict = not valid_metadata
            campaign_status = campaign.status
            campaign_updated_at = utc_text(campaign.updated_at)
            campaign_metadata_sha256 = hashlib.sha256(
                json.dumps(metadata, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            expected_binding = {
                "version": 1,
                "workflow_id": row.workflow_id,
                "first_execution_run_id": row.temporal_first_execution_run_id,
                "application_run_id": str(row.id),
            }
            raw_binding = metadata.get("reconciliation_binding")
            binding_present = safe_execution_binding(metadata) == expected_binding
            if raw_binding is not None and not binding_present:
                binding_conflict = True
            meta_workflow_id = metadata.get("workflow_id")
            if meta_workflow_id is not None and meta_workflow_id != row.workflow_id:
                binding_conflict = True

        candidate: dict[str, Any] = {
            "application_run_id": str(row.id),
            "workflow_id": row.workflow_id,
            "first_execution_run_id": row.temporal_first_execution_run_id,
            "run_count": run_count,
            "run_type": row.run_type,
            "project": row.project,
            "run_status": row.status,
            "run_error_code": row.error_code,
            "run_finished_at": utc_text(row.finished_at),
            "run_updated_at": utc_text(row.updated_at),
            "campaign_id": str(row.campaign_id) if row.campaign_id is not None else None,
            "campaign_status": campaign_status,
            "campaign_run_count": campaign_run_count,
            "campaign_updated_at": campaign_updated_at,
            "campaign_metadata_sha256": campaign_metadata_sha256,
            "campaign_binding_present": binding_present,
            "campaign_binding_conflict": binding_conflict,
        }
        candidate["snapshot_token"] = run_snapshot_token(candidate)
        return candidate

    def candidates(self, after: UUID | None, limit: int) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if after is not None and (type(after) is not UUID or after.int == 0):
            raise ValueError("after must not be the nil UUID")

        with self._sessions() as session:
            self._budget(session, write=False)

            eligible = or_(
                and_(Run.status.notin_(TERMINAL_OUTCOMES), Run.finished_at.is_(None)),
                and_(Run.run_type == _AUDIT_CAMPAIGN, Campaign.status.notin_(TERMINAL_OUTCOMES)),
            )
            stmt = select(Run).outerjoin(Campaign, Run.campaign_id == Campaign.id).where(eligible)
            if after is not None:
                stmt = stmt.where(Run.id > after)
            stmt = stmt.order_by(Run.id.asc()).limit(limit)

            rows = session.execute(stmt).scalars().all()

            campaign_ids = {
                row.campaign_id
                for row in rows
                if row.run_type == _AUDIT_CAMPAIGN and row.campaign_id is not None
            }
            campaigns: dict[UUID, Campaign] = {}
            if campaign_ids:
                campaign_rows = (
                    session.execute(select(Campaign).where(Campaign.id.in_(campaign_ids)))
                    .scalars()
                    .all()
                )
                campaigns = {c.id: c for c in campaign_rows}

            return [
                self._candidate(
                    session,
                    row,
                    campaigns.get(row.campaign_id)
                    if row.run_type == _AUDIT_CAMPAIGN and row.campaign_id is not None
                    else None,
                )
                for row in rows
            ]

    def refresh(self, run_id: UUID) -> dict[str, Any] | None:
        if type(run_id) is not UUID or run_id.int == 0:
            raise ValueError("invalid Run identity")
        with self._sessions() as session:
            self._budget(session, write=False)
            row = session.get(Run, run_id)
            if row is None:
                return None
            campaign = None
            if row.run_type == _AUDIT_CAMPAIGN and row.campaign_id is not None:
                campaign = session.get(Campaign, row.campaign_id)
            return self._candidate(session, row, campaign)

    def apply(self, expected: dict[str, Any], observed: dict[str, Any]) -> RunDecision:
        application_run_id = expected.get("application_run_id")
        if not isinstance(application_run_id, str):
            raise ValueError("expected.application_run_id must be a string UUID")
        run_id = UUID(application_run_id)
        if str(run_id) != application_run_id or run_id.int == 0:
            raise ValueError("invalid Run identity")

        if expected.get("snapshot_token") != run_snapshot_token(expected):
            raise ValueError("expected snapshot_token does not match candidate fields")

        expected_run_type = expected.get("run_type")
        expected_campaign_id = expected.get("campaign_id")

        with self._sessions.begin() as session:
            self._budget(session, write=True)

            campaign: Campaign | None = None
            if expected_run_type == _AUDIT_CAMPAIGN and expected_campaign_id is not None:
                campaign = session.execute(
                    select(Campaign)
                    .where(Campaign.id == UUID(expected_campaign_id))
                    .with_for_update()
                ).scalar_one_or_none()

            row = session.execute(
                select(Run).where(Run.id == run_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return RunDecision(classification="SNAPSHOT_CHANGED")

            current = self._candidate(
                session, row, campaign if expected_run_type == _AUDIT_CAMPAIGN else None
            )
            if current["snapshot_token"] != expected["snapshot_token"]:
                return RunDecision(classification="SNAPSHOT_CHANGED")

            if current["campaign_binding_conflict"]:
                return RunDecision(classification="AMBIGUOUS_CAMPAIGN_BINDING")
            if expected_run_type == _AUDIT_CAMPAIGN and not current["campaign_binding_present"]:
                return RunDecision(classification="EXECUTION_BINDING_UNVERIFIED")

            decision = decide_run_reconciliation(current, observed)

            if decision.classification == "TERMINAL_REPAIR_AVAILABLE":
                if row.error_code is not None and decision.target_error_code != row.error_code:
                    raise ValueError("reconciliation may not replace a persisted error")
                if (
                    decision.target_run_status is not None
                    and decision.target_run_status != row.status
                ):
                    row.status = decision.target_run_status
                if (
                    decision.target_error_code is not None
                    and decision.target_error_code != row.error_code
                ):
                    row.error_code = decision.target_error_code
                if (
                    campaign is not None
                    and decision.target_campaign_status is not None
                    and decision.target_campaign_status != campaign.status
                ):
                    campaign.status = decision.target_campaign_status

            return decision
