"""Typed, value-minimized requests for the private publication authority."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

RepositoryName = Literal[
    "gntik-ai/falcone",
    "gntik-ai/falcone-charts",
    "gntik-ai/musematic",
    "gntik-ai/musematic-deploy",
    "gntik-ai/llmwiki",
]


class IssueRevisionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: RepositoryName
    number: StrictInt = Field(ge=1, le=2_147_483_647)
    state: Literal["OPEN", "CLOSED"]
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    title_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    updated_at: AwareDatetime
    observed_at: AwareDatetime


class PublicationExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=240)
    workflow_run_id: UUID
    activity_id: str = Field(min_length=1, max_length=240)
    activity_run_id: UUID | None = None
    attempt: StrictInt = Field(ge=1, le=1000)


class PublicationEffectClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: UUID
    intent_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository: RepositoryName
    action: Literal["git_push", "pr_create"]
    execution: PublicationExecution
    observation: IssueRevisionObservation


class PublicationEffectReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effect_id: UUID
    claim_id: UUID
    outcome: Literal["COMPLETE", "UNKNOWN"]
    # An effect receipt stores only the digest of bounded typed output, not a
    # raw command transcript, issue body, credential, or caller-supplied status.
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PublicationClaimed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["CLAIMED"]
    effect_id: UUID
    claim_id: UUID
    start_before: AwareDatetime


class PublicationAlreadyComplete(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ALREADY_COMPLETE"]
    effect_id: UUID


class PublicationReceiptAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["COMPLETE", "UNKNOWN"]
    effect_id: UUID
