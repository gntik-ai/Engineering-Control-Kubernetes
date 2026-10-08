from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engineering_control.engineering.models import GitSHA, RepositoryName
from engineering_control.projects import PROJECT_REPOSITORIES, SELF_CONTAINED, ProjectName


class FindingPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectName
    classification: Literal["application", "deployment", "cross-repository", "falcone-contract-gap"]
    affected_repositories: list[RepositoryName] = Field(min_length=1, max_length=3)
    source_sha: GitSHA
    deployment_sha: GitSHA
    running_revision: str = Field(min_length=1, max_length=200)
    persona: str = Field(min_length=1, max_length=120)
    flow: str = Field(min_length=1, max_length=120)
    expected_behavior: str = Field(min_length=1, max_length=4000)
    actual_behavior: str = Field(min_length=1, max_length=4000)
    minimal_reproduction: list[str] = Field(min_length=1, max_length=30)
    evidence_handles: list[str] = Field(min_length=1, max_length=50)
    severity: str = Field(pattern=r"^(critical|high|medium|low)$")
    risk: Literal["R0", "R1", "R2", "R3"]
    fingerprint: str = Field(min_length=16, max_length=128)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=30)
    cleanup_result: str = Field(
        min_length=1,
        max_length=1000,
        pattern=r"^(?:PROVEN|NOT_APPLICABLE_[A-Z0-9_]+)$",
    )
    falcone_source_sha: GitSHA | None = None
    falcone_charts_sha: GitSHA | None = None
    llmwiki_sha: GitSHA | None = None
    consumer_contract: str | None = Field(default=None, max_length=500)
    staging_target: str | None = Field(default=None, max_length=253)

    @model_validator(mode="after")
    def consumer_binding(self) -> FindingPublication:
        if self.classification == "falcone-contract-gap" and not all(
            (
                self.falcone_source_sha,
                self.falcone_charts_sha,
                self.llmwiki_sha,
                self.consumer_contract,
                self.staging_target,
            )
        ):
            raise ValueError(
                "Falcone consumer contract gaps require three immutable SHAs and target"
            )
        return self

    def primary_repository(self) -> str:
        if self.classification == "falcone-contract-gap":
            return "gntik-ai/falcone"
        source, deployment = PROJECT_REPOSITORIES[self.project]
        if self.project in SELF_CONTAINED:
            return source
        if self.classification == "application":
            return source
        if self.classification == "deployment":
            return deployment
        if source in self.affected_repositories:
            return source
        return deployment

    def body(self) -> str:
        reproduction = "\n".join(
            f"{index}. {step}" for index, step in enumerate(self.minimal_reproduction, 1)
        )
        criteria = "\n".join(f"- [ ] {item}" for item in self.acceptance_criteria)
        evidence = "\n".join(f"- `{handle}`" for handle in self.evidence_handles)
        repositories = ", ".join(f"`{item}`" for item in self.affected_repositories)
        return (
            f"## Engineering Control finding\n\n"
            f"- Project: `{self.project}`\n"
            f"- Affected repositories: {repositories}\n"
            f"- Source SHA: `{self.source_sha}`\n"
            f"- Deployment SHA: `{self.deployment_sha}`\n"
            f"- Running revision/digest: `{self.running_revision}`\n"
            f"- Persona: `{self.persona}`\n"
            f"- Flow: `{self.flow}`\n"
            f"- Severity / risk: `{self.severity}` / `{self.risk}`\n"
            f"- Fingerprint: `{self.fingerprint}`\n\n"
            f"- Consumer contract: `{self.consumer_contract or 'not-applicable'}`\n"
            f"- Staging target: `{self.staging_target or 'not-recorded'}`\n\n"
            f"## Expected behavior\n\n{self.expected_behavior}\n\n"
            f"## Actual behavior\n\n{self.actual_behavior}\n\n"
            f"## Minimal reproduction\n\n{reproduction}\n\n"
            f"## Evidence handles\n\n{evidence}\n\n"
            f"## Acceptance criteria\n\n{criteria}\n\n"
            f"## Cleanup result\n\n{self.cleanup_result}\n"
        )


def finding_fingerprint(value: dict[str, object]) -> str:
    canonical = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return "finding-" + hashlib.sha256(canonical).hexdigest()
