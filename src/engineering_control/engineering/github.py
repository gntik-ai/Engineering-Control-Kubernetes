from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engineering_control.engineering.models import GitSHA, RepositoryName


class FindingPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: Literal["falcone", "musematic"]
    classification: Literal["application", "deployment", "cross-repository"]
    affected_repositories: list[RepositoryName] = Field(min_length=1, max_length=2)
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
    cleanup_result: str = Field(min_length=1, max_length=1000)

    def primary_repository(self) -> str:
        source = f"gntik-ai/{self.project}"
        deployment = (
            "gntik-ai/falcone-charts" if self.project == "falcone" else "gntik-ai/musematic-deploy"
        )
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
