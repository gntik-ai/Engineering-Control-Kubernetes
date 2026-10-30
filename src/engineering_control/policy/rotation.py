"""NEW-243 phase 1: provider rotation for agent roles (delivery roles first).

A pure, deterministic policy. Which provider (Codex CLI or Claude CLI) plays an *author*
role is a hash of (project, flow, role, subject) weighted by the project's ratio, unless a
per-role pin forces it; when the chosen provider is unavailable under today's quota rules
the other one runs and the fallback is recorded. A *reviewer* role is always the opposite
provider of the author it reviews: it is never pinned, never weighted and never falls back
to the author's provider (unavailable means today's waiting/blocking).

Activities call :func:`choose` (never workflow code) and record the returned
:class:`RotationDecision`; a delivery's maker is chosen once per ChangeSet and then read back
from that record for every retry, correction and CI repair (see activities/metadata.py
``_route_delivery``).

Configuration lives in the project manifest (``provider_rotation``), re-read on every
activity, so changing a ratio or a pin needs no deploy. Absent or ``enabled: false`` keeps
today's routing unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engineering_control.models.enums import Provider

ProviderName = Literal["codex", "claude"]

# Delivery roles (phase 1). Authors produce work; reviewers judge an author's work.
IMPLEMENTER = "implementer"
SPEC_ARCHITECT = "product-spec-architect"
CODE_REVIEWER = "code-reviewer"
AUTHOR_ROLES = frozenset({IMPLEMENTER, SPEC_ARCHITECT})
REVIEWER_ROLES = frozenset({CODE_REVIEWER})
ROLES = AUTHOR_ROLES | REVIEWER_ROLES


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RotationRatio(_Model):
    """Relative weights of the two providers for author roles (e.g. 1:1 for 50/50)."""

    codex: int = Field(default=1, ge=0, le=1000)
    claude: int = Field(default=1, ge=0, le=1000)

    @model_validator(mode="after")
    def some_weight(self) -> RotationRatio:
        if self.codex + self.claude == 0:
            raise ValueError("provider_rotation.ratio needs a positive weight")
        return self


class RoleModels(_Model):
    codex: str | None = Field(default=None, min_length=1, max_length=120)
    claude: str | None = Field(default=None, min_length=1, max_length=120)


class RoleRotation(_Model):
    pin: ProviderName | None = None
    models: RoleModels = RoleModels()


class ProviderRotationPolicy(_Model):
    """``provider_rotation`` in a project manifest.

    ``enabled_at``: rotation applies only to ChangeSets created at or after this instant;
    older (in-flight) ones keep today's routing and their recorded providers."""

    enabled: bool = False
    enabled_at: datetime | None = None
    ratio: RotationRatio = RotationRatio()
    roles: dict[str, RoleRotation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> ProviderRotationPolicy:
        if self.enabled and self.enabled_at is None:
            raise ValueError("provider_rotation.enabled_at is required when enabled")
        if self.enabled_at is not None and self.enabled_at.tzinfo is None:
            raise ValueError("provider_rotation.enabled_at needs a time zone")
        unknown = sorted(set(self.roles) - ROLES)
        if unknown:
            raise ValueError(f"provider_rotation.roles: unknown role(s) {unknown}")
        pinned_reviewers = sorted(
            role for role in REVIEWER_ROLES if role in self.roles and self.roles[role].pin
        )
        if pinned_reviewers:
            raise ValueError(
                f"provider_rotation.roles: reviewer role(s) {pinned_reviewers} cannot be "
                "pinned (a reviewer is always the opposite provider of its author)"
            )
        return self

    def policy_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def model_for(self, role: str, provider: Provider) -> str | None:
        configured = self.roles.get(role)
        if configured is None:
            return None
        if provider is Provider.CODEX:
            return configured.models.codex
        return configured.models.claude


class QuotaView(Protocol):
    """Today's availability rules (quota/broker.py ``QuotaBroker.available``)."""

    def available(self, provider: Provider, *, reserve_work: bool) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class RotationDecision:
    """Who plays one role for one subject, and why. ``provider`` None: nobody can now
    (``status`` RATE_LIMITED for an author, BLOCKED for a reviewer)."""

    role: str
    provider: Provider | None
    model: str | None
    reason: str
    policy_hash: str
    fallback: str | None = None
    status: Literal["READY", "RATE_LIMITED", "BLOCKED"] = "READY"

    def record(self) -> dict[str, str | None]:
        return {
            "role": self.role,
            "provider": self.provider.value if self.provider is not None else None,
            "model": self.model,
            "reason": self.reason,
            "policy_hash": self.policy_hash,
            "fallback": self.fallback,
        }


def opposite(provider: Provider) -> Provider:
    return Provider.CLAUDE if provider is Provider.CODEX else Provider.CODEX


def hashed_provider(
    policy: ProviderRotationPolicy, project: str, flow: str, role: str, subject_id: str
) -> Provider:
    """The ratio-weighted provider for one (project, flow, role, subject): stable across
    processes and Python versions (SHA-256, never ``hash()``)."""
    digest = hashlib.sha256(f"{project}|{flow}|{role}|{subject_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % (policy.ratio.codex + policy.ratio.claude)
    return Provider.CODEX if bucket < policy.ratio.codex else Provider.CLAUDE


def choose(
    policy: ProviderRotationPolicy,
    *,
    project: str,
    flow: str,
    role: str,
    subject_id: str,
    quota: QuotaView,
    author: Provider | None = None,
    reserve_work: bool = False,
) -> RotationDecision:
    """Choose the provider (and its configured model) for ``role`` on ``subject_id``.

    Reviewer roles need ``author`` and get its opposite or nothing. Author roles get the
    pin, else the ratio-weighted hash; an unavailable choice falls back to the other
    provider (recorded) and, when both are unavailable, to nothing."""
    digest = policy.policy_hash()
    if role in REVIEWER_ROLES:
        if author is None:
            raise ValueError(f"reviewer role {role} needs the author's provider")
        reviewer = opposite(author)
        ok, why = quota.available(reviewer, reserve_work=True)
        if not ok:
            return RotationDecision(
                role,
                None,
                None,
                f"reviewer {reviewer.value} (opposite of {author.value}) unavailable: {why}",
                digest,
                status="BLOCKED",
            )
        return RotationDecision(
            role,
            reviewer,
            policy.model_for(role, reviewer),
            f"opposite of {author.value};{reviewer.value}:{why}",
            digest,
        )
    if role not in AUTHOR_ROLES:
        raise ValueError(f"role {role} is not rotated")
    configured = policy.roles.get(role)
    if configured is not None and configured.pin is not None:
        wanted, how = Provider(configured.pin), "pin"
    else:
        wanted, how = hashed_provider(policy, project, flow, role, subject_id), "ratio"
    ok, why = quota.available(wanted, reserve_work=reserve_work)
    if ok:
        return RotationDecision(
            role, wanted, policy.model_for(role, wanted), f"{how}:{wanted.value}:{why}", digest
        )
    other = opposite(wanted)
    other_ok, other_why = quota.available(other, reserve_work=reserve_work)
    if not other_ok:
        return RotationDecision(
            role,
            None,
            None,
            f"{how}:{wanted.value}:{why};{other.value}:{other_why}",
            digest,
            status="RATE_LIMITED",
        )
    return RotationDecision(
        role,
        other,
        policy.model_for(role, other),
        f"{how}:{wanted.value}:{why};fallback:{other.value}:{other_why}",
        digest,
        fallback=f"{wanted.value}:{why}",
    )
