from datetime import UTC, datetime, timedelta

from engineering_control.models.enums import ModelClass, Provider, RiskClass
from engineering_control.quota.broker import QuotaBroker, QuotaSnapshot


def snapshot(provider: Provider, fraction: float, *, limited: bool = False) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider=provider,
        captured_at=datetime.now(UTC),
        remaining_capacity=fraction * 100,
        total_capacity=100,
        reset_at=datetime.now(UTC) + timedelta(days=1),
        running_jobs=0,
        rate_limited=limited,
    )


def test_reserve_is_protected_for_implementation_but_available_to_review() -> None:
    broker = QuotaBroker(reserve_fraction=0.25)
    broker.record(snapshot(Provider.CODEX, 0.20))
    broker.record(snapshot(Provider.CLAUDE, 0.20))
    implementation = broker.route(
        role="implementer", model_class=ModelClass.WORKER, risk_class=RiskClass.R1
    )
    assert implementation.status == "RATE_LIMITED"
    review = broker.route(
        role="code-reviewer", model_class=ModelClass.JUDGE, risk_class=RiskClass.R1
    )
    assert review.status == "READY"


def test_r3_blocks_when_cross_provider_unavailable() -> None:
    broker = QuotaBroker()
    broker.record(snapshot(Provider.CODEX, 0.80))
    broker.record(snapshot(Provider.CLAUDE, 0.80, limited=True))
    decision = broker.route(
        role="implementer", model_class=ModelClass.WORKER, risk_class=RiskClass.R3
    )
    assert decision.primary is Provider.CODEX
    assert decision.status == "BLOCKED"
    assert decision.checker is None


def test_unknown_capacity_is_not_invented() -> None:
    decision = QuotaBroker().route(
        role="implementer", model_class=ModelClass.WORKER, risk_class=RiskClass.R1
    )
    assert decision.status == "READY"
    assert "capacity_unknown" in decision.reason
