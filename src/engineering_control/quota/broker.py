from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from engineering_control.models.enums import ModelClass, Provider, RiskClass


@dataclass(frozen=True)
class QuotaSnapshot:
    provider: Provider
    captured_at: datetime
    remaining_capacity: float | None
    total_capacity: float | None
    reset_at: datetime | None
    running_jobs: int
    rate_limited: bool = False
    source: str = "native_cli"

    @property
    def remaining_fraction(self) -> float | None:
        if self.remaining_capacity is None or not self.total_capacity:
            return None
        return max(0.0, min(1.0, self.remaining_capacity / self.total_capacity))


@dataclass(frozen=True)
class RoutingDecision:
    primary: Provider | None
    checker: Provider | None
    model_class: ModelClass
    status: str
    reason: str
    handoff_required: bool = False


class QuotaBroker:
    def __init__(self, reserve_fraction: float = 0.25) -> None:
        if not 0.20 <= reserve_fraction <= 0.30:
            raise ValueError("verification reserve must remain between 20% and 30%")
        self.reserve_fraction = reserve_fraction
        self._snapshots: dict[Provider, QuotaSnapshot] = {}

    def record(self, snapshot: QuotaSnapshot) -> None:
        existing = self._snapshots.get(snapshot.provider)
        if existing and snapshot.captured_at < existing.captured_at:
            raise ValueError("quota snapshots cannot move backwards in time")
        self._snapshots[snapshot.provider] = snapshot

    def available(self, provider: Provider, *, reserve_work: bool) -> tuple[bool, str]:
        snapshot = self._snapshots.get(provider)
        if snapshot is None:
            return True, "capacity_unknown"
        if snapshot.rate_limited:
            if snapshot.reset_at and snapshot.reset_at <= datetime.now(UTC):
                return True, "rate_limit_reset_due_refresh"
            return False, "provider_rate_limited"
        fraction = snapshot.remaining_fraction
        if fraction is not None and not reserve_work and fraction <= self.reserve_fraction:
            return False, "verification_reserve_protected"
        return True, "available" if fraction is not None else "capacity_unknown"

    def route(
        self,
        *,
        role: str,
        model_class: ModelClass,
        risk_class: RiskClass,
        maker: Provider | None = None,
    ) -> RoutingDecision:
        reserve_roles = {
            "finding-verifier",
            "code-reviewer",
            "runtime-verifier",
            "security-auditor",
            "incident-analyst",
            "release-plan-reviewer",
        }
        reserve_work = role in reserve_roles or risk_class is RiskClass.R3
        if maker is not None:
            preferences = [Provider.CLAUDE if maker is Provider.CODEX else Provider.CODEX]
        elif role in {"product-spec-architect", "campaign-planner", "incident-analyst"}:
            preferences = [Provider.CLAUDE, Provider.CODEX]
        elif role in {"implementer", "repository-cartographer"}:
            preferences = [Provider.CODEX, Provider.CLAUDE]
        else:
            preferences = [Provider.CODEX, Provider.CLAUDE]

        chosen: Provider | None = None
        reasons: list[str] = []
        for provider in preferences:
            ok, reason = self.available(provider, reserve_work=reserve_work)
            reasons.append(f"{provider.value}:{reason}")
            if ok:
                chosen = provider
                break
        if chosen is None:
            return RoutingDecision(
                None,
                None,
                model_class,
                "RATE_LIMITED",
                ";".join(reasons),
                handoff_required=True,
            )

        opposite = Provider.CLAUDE if chosen is Provider.CODEX else Provider.CODEX
        checker: Provider | None = opposite
        checker_ok, checker_reason = self.available(opposite, reserve_work=True)
        if not checker_ok:
            checker = None
        if risk_class is RiskClass.R3 and checker is None:
            return RoutingDecision(
                chosen,
                None,
                model_class,
                "BLOCKED",
                f"R3 requires cross-provider verification; {checker_reason}",
                handoff_required=True,
            )
        return RoutingDecision(
            chosen,
            checker,
            model_class,
            "READY",
            ";".join((*reasons, f"checker:{checker_reason}")),
        )
