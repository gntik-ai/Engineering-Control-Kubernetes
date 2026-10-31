"""NEW-243 phase 1: provider rotation for the delivery roles.

The policy is pure and deterministic; the delivery routing chooses a ChangeSet's maker once,
records it, and reads it back on every later attempt; reviewers are always the opposite
provider; a project without an enabled policy keeps today's routing byte for byte.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import ChangeSet, Run
from engineering_control.engineering.models import ProjectManifest
from engineering_control.models.enums import Provider
from engineering_control.policy.rotation import (
    CODE_REVIEWER,
    IMPLEMENTER,
    REVIEWER_ROLES,
    SPEC_ARCHITECT,
    ProviderRotationPolicy,
    choose,
    hashed_provider,
    opposite,
)
from engineering_control.quota.broker import QuotaBroker, QuotaSnapshot

ROOT = Path(__file__).resolve().parents[1]
# The provider the pinned subject below hashes to (SHA-256 based, see the test).
EXPECTED_STABLE_PROVIDER = Provider.CODEX
ENABLED_AT = datetime(2026, 10, 1, tzinfo=UTC)


class Quota:
    """Availability per provider, as QuotaBroker.available answers it."""

    def __init__(self, codex: bool = True, claude: bool = True) -> None:
        self.state = {Provider.CODEX: codex, Provider.CLAUDE: claude}

    def available(self, provider: Provider, *, reserve_work: bool) -> tuple[bool, str]:
        return (True, "capacity_unknown") if self.state[provider] else (False, "rate_limited")


def policy(**overrides: Any) -> ProviderRotationPolicy:
    value: dict[str, Any] = {"enabled": True, "enabled_at": ENABLED_AT.isoformat()}
    value.update(overrides)
    return ProviderRotationPolicy.model_validate(value)


# --- the pure policy ----------------------------------------------------------------------


def test_choice_is_deterministic_and_stable_across_processes() -> None:
    rotation = policy()
    subject = "6b0c9a52-0f5e-4f0e-9a51-6f4a2b1f2c3d"
    first = choose(
        rotation,
        project="gntik-ui",
        flow="delivery",
        role=IMPLEMENTER,
        subject_id=subject,
        quota=Quota(),
    )
    again = choose(
        rotation,
        project="gntik-ui",
        flow="delivery",
        role=IMPLEMENTER,
        subject_id=subject,
        quota=Quota(),
    )
    assert first == again
    # SHA-256 of "gntik-ui|delivery|implementer|<subject>", never Python's salted hash():
    # pinned so a change of algorithm (which would re-assign live ChangeSets) is noticed.
    assert hashed_provider(rotation, "gntik-ui", "delivery", IMPLEMENTER, subject) is (
        first.provider
    )
    assert first.provider is EXPECTED_STABLE_PROVIDER
    assert first.policy_hash == rotation.policy_hash() and len(first.policy_hash) == 64


@pytest.mark.parametrize(("codex", "claude"), [(1, 1), (3, 1), (1, 4), (1, 0), (0, 1)])
def test_the_ratio_holds_over_many_subjects(codex: int, claude: int) -> None:
    rotation = policy(ratio={"codex": codex, "claude": claude})
    count = 10_000
    chosen = [
        hashed_provider(rotation, "gntik-ui", "delivery", IMPLEMENTER, str(uuid4()))
        for _ in range(count)
    ]
    share = chosen.count(Provider.CODEX) / count
    assert abs(share - codex / (codex + claude)) < 0.03


def test_roles_and_projects_choose_independently() -> None:
    rotation = policy()
    subjects = [str(uuid4()) for _ in range(400)]
    maker = [hashed_provider(rotation, "p", "delivery", IMPLEMENTER, s) for s in subjects]
    spec = [hashed_provider(rotation, "p", "delivery", SPEC_ARCHITECT, s) for s in subjects]
    other = [hashed_provider(rotation, "q", "delivery", IMPLEMENTER, s) for s in subjects]
    assert maker != spec and maker != other


def test_a_reviewer_is_always_the_opposite_provider_of_its_author() -> None:
    """Property-style: every author, quota state, pin and ratio; a reviewer is the
    author's opposite or nobody, never the author's own provider."""
    for author, codex_ok, claude_ok, pin, ratio in itertools.product(
        (Provider.CODEX, Provider.CLAUDE),
        (True, False),
        (True, False),
        (None, "codex", "claude"),
        ((1, 1), (1, 0), (0, 1)),
    ):
        roles: dict[str, Any] = {IMPLEMENTER: {"pin": pin}} if pin else {}
        rotation = policy(ratio={"codex": ratio[0], "claude": ratio[1]}, roles=roles)
        for subject in (str(uuid4()) for _ in range(5)):
            for role in REVIEWER_ROLES:
                decision = choose(
                    rotation,
                    project="p",
                    flow="delivery",
                    role=role,
                    subject_id=subject,
                    quota=Quota(codex_ok, claude_ok),
                    author=author,
                )
                assert decision.provider in {opposite(author), None}
                if decision.provider is None:
                    assert decision.status == "BLOCKED" and decision.fallback is None


def test_a_reviewer_needs_its_author() -> None:
    with pytest.raises(ValueError, match="needs the author"):
        choose(
            policy(),
            project="p",
            flow="delivery",
            role=CODE_REVIEWER,
            subject_id="s",
            quota=Quota(),
        )


def test_a_pin_forces_the_author_provider() -> None:
    rotation = policy(roles={IMPLEMENTER: {"pin": "claude"}})
    for _ in range(50):
        decision = choose(
            rotation,
            project="p",
            flow="delivery",
            role=IMPLEMENTER,
            subject_id=str(uuid4()),
            quota=Quota(),
        )
        assert decision.provider is Provider.CLAUDE and decision.reason.startswith("pin:")


def test_an_unavailable_choice_falls_back_and_says_so() -> None:
    rotation = policy(roles={IMPLEMENTER: {"pin": "codex"}})
    fallback = choose(
        rotation,
        project="p",
        flow="delivery",
        role=IMPLEMENTER,
        subject_id="s",
        quota=Quota(codex=False),
    )
    assert fallback.provider is Provider.CLAUDE
    assert fallback.fallback == "codex:rate_limited" and "fallback:claude" in fallback.reason
    nobody = choose(
        rotation,
        project="p",
        flow="delivery",
        role=IMPLEMENTER,
        subject_id="s",
        quota=Quota(codex=False, claude=False),
    )
    assert nobody.provider is None and nobody.status == "RATE_LIMITED"


def test_models_come_from_the_role_configuration() -> None:
    rotation = policy(
        roles={
            IMPLEMENTER: {"models": {"claude": "claude-sonnet-5-5", "codex": "gpt-6.1-sol"}},
            CODE_REVIEWER: {"models": {"claude": "claude-opus-5-5"}},
        }
    )
    assert rotation.model_for(IMPLEMENTER, Provider.CLAUDE) == "claude-sonnet-5-5"
    assert rotation.model_for(CODE_REVIEWER, Provider.CODEX) is None  # today's default
    assert rotation.model_for(SPEC_ARCHITECT, Provider.CLAUDE) is None
    reviewer = choose(
        rotation,
        project="p",
        flow="delivery",
        role=CODE_REVIEWER,
        subject_id="s",
        quota=Quota(),
        author=Provider.CODEX,
    )
    assert reviewer.provider is Provider.CLAUDE and reviewer.model == "claude-opus-5-5"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"enabled": True}, "enabled_at is required"),
        ({"enabled": True, "enabled_at": "2026-10-01T00:00:00"}, "time zone"),
        ({"ratio": {"codex": 0, "claude": 0}}, "positive weight"),
        ({"ratio": {"codex": -1}}, "greater than or equal"),
        ({"roles": {"maker": {}}}, "unknown role"),
        ({"roles": {CODE_REVIEWER: {"pin": "codex"}}}, "cannot be pinned"),
        ({"roles": {IMPLEMENTER: {"pin": "gemini"}}}, "codex"),
        ({"roles": {IMPLEMENTER: {"models": {"codex": ""}}}}, "at least 1"),
        ({"enable": True}, "Extra inputs"),
    ],
)
def test_configuration_is_validated(value: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ProviderRotationPolicy.model_validate(value)


def test_every_shipped_project_manifest_still_validates() -> None:
    for path in sorted((ROOT / "config" / "projects").glob("*.yaml")):
        ProjectManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- delivery routing ---------------------------------------------------------------------


def _handler(tmp_path: Path, rotation: dict[str, Any] | None) -> MetadataActivityHandler:
    config = tmp_path / "projects"
    config.mkdir()
    manifest = yaml.safe_load((ROOT / "config/projects/falcone.yaml").read_text())
    manifest.pop("provider_rotation", None)
    if rotation is not None:
        manifest["provider_rotation"] = rotation
    (config / "falcone.yaml").write_text(yaml.safe_dump(manifest))
    handler = MetadataActivityHandler(
        Settings(
            database_url=SecretStr(f"sqlite:///{tmp_path / 'rotation.sqlite'}"),
            project_config_root=config,
        )
    )
    Base.metadata.create_all(handler.engine)
    return handler


def _change_set(
    handler: MetadataActivityHandler,
    *,
    created_at: datetime = ENABLED_AT + timedelta(days=1),
    recovery: dict[str, Any] | None = None,
    earlier_runs: int = 0,
) -> tuple[UUID, UUID]:
    change_set_id, run_id = uuid4(), uuid4()
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R2",
                state="CONFIRMED",
                version=1,
                source_repository="falcone",
                source_branch=None,
                source_base_sha="a" * 40,
                source_head_sha=None,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch=None,
                deployment_base_sha="b" * 40,
                deployment_head_sha=None,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json=recovery or {},
                created_at=created_at,
            )
        )
        for index in range(earlier_runs + 1):
            session.add(
                Run(
                    id=run_id if index == 0 else uuid4(),
                    campaign_id=None,
                    change_set_id=change_set_id,
                    workflow_id=f"delivery-{change_set_id}-{index}",
                    run_type="issue-delivery",
                    status="RUNNING",
                    project="falcone",
                )
            )
    return change_set_id, run_id


def _payload(change_set_id: UUID, run_id: UUID, risk: str = "R2") -> dict[str, Any]:
    return {
        "change_set_id": str(change_set_id),
        "run_id": str(run_id),
        "project": "falcone",
        "risk_class": risk,
        "fingerprint": "f" * 64,
        "source_repository": "falcone",
        "source_base_sha": "a" * 40,
        "source_base_ref": "refs/heads/main",
        "deployment_repository": "falcone-charts",
        "deployment_base_sha": "b" * 40,
        "deployment_base_ref": "refs/heads/main",
    }


def _broker(codex: bool | float = True, claude: bool | float = True) -> QuotaBroker:
    """True: available; False: rate limited; a float: remaining capacity fraction."""
    broker = QuotaBroker()
    now = datetime.now(UTC)
    for provider, state in ((Provider.CODEX, codex), (Provider.CLAUDE, claude)):
        if state is True:
            continue
        broker.record(
            QuotaSnapshot(
                provider=provider,
                captured_at=now,
                remaining_capacity=None if state is False else float(state) * 100,
                total_capacity=None if state is False else 100.0,
                reset_at=now + timedelta(hours=1),
                running_jobs=0,
                rate_limited=state is False,
            )
        )
    return broker


def _route(handler: MetadataActivityHandler, payload: dict[str, Any], broker: QuotaBroker) -> Any:
    handler._quota_broker = lambda: broker  # type: ignore[method-assign]
    return handler._route_delivery(payload)


def _recovery(handler: MetadataActivityHandler, change_set_id: UUID) -> dict[str, Any]:
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None
        return dict(change_set.recovery_json)


QUOTA_STATES = list(itertools.product((True, False, 0.1), (True, False, 0.1)))

# Today's decisions, generated by running origin/main e26d959's _route_delivery (before
# NEW-243 phase 1) over this quota matrix, then pinned here:
# (codex state, claude state, risk) -> (status, maker, checker, spec, routing_reason).
TODAY = {
    (True, True, "R2"): (
        "ROUTED",
        "codex",
        "claude",
        "claude",
        "maker=codex:capacity_unknown;checker:capacity_unknown;spec=claude:capacity_unknown;checker:capacity_unknown",  # noqa: E501
    ),
    (True, True, "R3"): (
        "ROUTED",
        "codex",
        "claude",
        "claude",
        "maker=codex:capacity_unknown;checker:capacity_unknown;spec=claude:capacity_unknown;checker:capacity_unknown",  # noqa: E501
    ),
    (True, False, "R2"): (
        "BLOCKED",
        None,
        None,
        None,
        "maker=codex:capacity_unknown;checker:provider_rate_limited;spec=claude:provider_rate_limited;codex:capacity_unknown;checker:provider_rate_limited",  # noqa: E501
    ),
    (True, False, "R3"): (
        "BLOCKED",
        None,
        None,
        None,
        "maker=R3 requires cross-provider verification; provider_rate_limited;spec=R3 requires cross-provider verification; provider_rate_limited",  # noqa: E501
    ),
    (True, 0.1, "R2"): (
        "ROUTED",
        "codex",
        "claude",
        "codex",
        "maker=codex:capacity_unknown;checker:available;spec=claude:verification_reserve_protected;codex:capacity_unknown;checker:available",  # noqa: E501
    ),
    (True, 0.1, "R3"): (
        "ROUTED",
        "codex",
        "claude",
        "claude",
        "maker=codex:capacity_unknown;checker:available;spec=claude:available;checker:capacity_unknown",  # noqa: E501
    ),
    (False, True, "R2"): (
        "BLOCKED",
        None,
        None,
        None,
        "maker=codex:provider_rate_limited;claude:capacity_unknown;checker:provider_rate_limited;spec=claude:capacity_unknown;checker:provider_rate_limited",  # noqa: E501
    ),
    (False, True, "R3"): (
        "BLOCKED",
        None,
        None,
        None,
        "maker=R3 requires cross-provider verification; provider_rate_limited;spec=R3 requires cross-provider verification; provider_rate_limited",  # noqa: E501
    ),
    (False, False, "R2"): (
        "RATE_LIMITED",
        None,
        None,
        None,
        "maker=codex:provider_rate_limited;claude:provider_rate_limited;spec=claude:provider_rate_limited;codex:provider_rate_limited",  # noqa: E501
    ),
    (False, False, "R3"): (
        "RATE_LIMITED",
        None,
        None,
        None,
        "maker=codex:provider_rate_limited;claude:provider_rate_limited;spec=claude:provider_rate_limited;codex:provider_rate_limited",  # noqa: E501
    ),
    (False, 0.1, "R2"): (
        "RATE_LIMITED",
        None,
        None,
        None,
        "maker=codex:provider_rate_limited;claude:verification_reserve_protected;spec=claude:verification_reserve_protected;codex:provider_rate_limited",  # noqa: E501
    ),
    (False, 0.1, "R3"): (
        "BLOCKED",
        None,
        None,
        None,
        "maker=R3 requires cross-provider verification; provider_rate_limited;spec=R3 requires cross-provider verification; provider_rate_limited",  # noqa: E501
    ),
    (0.1, True, "R2"): (
        "ROUTED",
        "claude",
        "codex",
        "claude",
        "maker=codex:verification_reserve_protected;claude:capacity_unknown;checker:available;spec=claude:capacity_unknown;checker:available",  # noqa: E501
    ),
    (0.1, True, "R3"): (
        "ROUTED",
        "codex",
        "claude",
        "claude",
        "maker=codex:available;checker:capacity_unknown;spec=claude:capacity_unknown;checker:available",  # noqa: E501
    ),
    (0.1, False, "R2"): (
        "RATE_LIMITED",
        None,
        None,
        None,
        "maker=codex:verification_reserve_protected;claude:provider_rate_limited;spec=claude:provider_rate_limited;codex:verification_reserve_protected",  # noqa: E501
    ),
    (0.1, False, "R3"): (
        "BLOCKED",
        None,
        None,
        None,
        "maker=R3 requires cross-provider verification; provider_rate_limited;spec=R3 requires cross-provider verification; provider_rate_limited",  # noqa: E501
    ),
    (0.1, 0.1, "R2"): (
        "RATE_LIMITED",
        None,
        None,
        None,
        "maker=codex:verification_reserve_protected;claude:verification_reserve_protected;spec=claude:verification_reserve_protected;codex:verification_reserve_protected",  # noqa: E501
    ),
    (0.1, 0.1, "R3"): (
        "ROUTED",
        "codex",
        "claude",
        "claude",
        "maker=codex:available;checker:available;spec=claude:available;checker:available",
    ),
}


@pytest.mark.parametrize(
    "rotation",
    [
        None,  # no provider_rotation block
        {"enabled": False},
        {"enabled": False, "enabled_at": ENABLED_AT.isoformat(), "ratio": {"codex": 0}},
    ],
    ids=["absent", "disabled", "disabled-with-settings"],
)
def test_disabled_rotation_keeps_todays_routing(
    tmp_path: Path, rotation: dict[str, Any] | None
) -> None:
    handler = _handler(tmp_path, rotation)
    for (codex, claude), risk in itertools.product(QUOTA_STATES, ("R2", "R3")):
        change_set_id, run_id = _change_set(handler)
        payload = _payload(change_set_id, run_id, risk)
        routed = _route(handler, payload, _broker(codex, claude))
        status, maker, checker, spec, reason = TODAY[(codex, claude, risk)]
        assert routed["status"] == status, (codex, claude, risk)
        assert routed["routing_reason"] == reason
        assert "rotation" not in routed
        assert routed.get("maker_provider") == maker
        assert routed.get("checker_provider") == checker
        assert routed.get("spec_provider") == spec
        # Nothing is recorded for a ChangeSet that is not rotated.
        assert _recovery(handler, change_set_id) == {}
        # The whole result equals the routing with rotation removed from the code path.
        handler._delivery_rotation = lambda *_: None  # type: ignore[method-assign]
        assert _route(handler, payload, _broker(codex, claude)) == routed
        del handler._delivery_rotation


def test_enabled_rotation_records_the_choice_once(tmp_path: Path) -> None:
    handler = _handler(
        tmp_path,
        {
            "enabled": True,
            "enabled_at": ENABLED_AT.isoformat(),
            "ratio": {"codex": 1, "claude": 1},
            "roles": {
                IMPLEMENTER: {"models": {"claude": "claude-sonnet-5-5", "codex": "gpt-6.1-sol"}},
                CODE_REVIEWER: {"models": {"claude": "claude-opus-5-5"}},
            },
        },
    )
    makers = set()
    for _ in range(40):
        change_set_id, run_id = _change_set(handler)
        routed = _route(handler, _payload(change_set_id, run_id), _broker())
        assert routed["status"] == "ROUTED"
        maker, checker = Provider(routed["maker_provider"]), Provider(routed["checker_provider"])
        makers.add(maker)
        assert checker is opposite(maker)
        assert routed["allocation_request"]["maker_provider"] == maker.value
        record = _recovery(handler, change_set_id)["rotation"]
        assert record["maker"]["provider"] == maker.value
        assert record["checker"]["provider"] == checker.value
        assert record["maker"]["model"] == (
            "claude-sonnet-5-5" if maker is Provider.CLAUDE else "gpt-6.1-sol"
        )
        assert record["checker"]["model"] == (
            "claude-opus-5-5" if checker is Provider.CLAUDE else None
        )
        assert routed["rotation"]["maker"]["model"] == record["maker"]["model"]
        assert routed["routing_reason"].startswith(f"rotation:{record['policy_hash'][:12]};chosen")
        assert f"maker={maker.value}/" in routed["routing_reason"]
    assert makers == {Provider.CODEX, Provider.CLAUDE}  # 40 subjects at 50/50


def test_the_maker_is_pinned_for_every_later_attempt(tmp_path: Path) -> None:
    rotation = {"enabled": True, "enabled_at": ENABLED_AT.isoformat()}
    handler = _handler(tmp_path, rotation)
    change_set_id, run_id = _change_set(handler)
    first = _route(handler, _payload(change_set_id, run_id), _broker())
    maker = Provider(first["maker_provider"])
    # A correction / CI repair is another run of the same ChangeSet.
    for attempt in range(5):
        retry = _payload(change_set_id, uuid4())
        again = _route(handler, retry, _broker())
        assert (again["maker_provider"], again["checker_provider"], again["spec_provider"]) == (
            first["maker_provider"],
            first["checker_provider"],
            first["spec_provider"],
        ), attempt
        assert ";pinned;" in again["routing_reason"]
    # The pinned maker's provider is rate limited: wait, never flip.
    limited = _broker(**{maker.value: False})
    waiting = _route(handler, _payload(change_set_id, uuid4()), limited)
    assert waiting["status"] == "RATE_LIMITED"
    assert waiting["routing_reason"].startswith(f"rotation:pinned maker {maker.value} unavailable")
    assert "maker_provider" not in waiting
    # The opposite (the checker) is unavailable: today's blocking.
    blocked = _route(
        handler, _payload(change_set_id, uuid4()), _broker(**{opposite(maker).value: False})
    )
    assert blocked["status"] == "BLOCKED"
    # A changed ratio or a pin does not move a pinned ChangeSet (in flight)...
    other = opposite(maker).value
    handler2_config = {
        **rotation,
        "roles": {IMPLEMENTER: {"pin": other, "models": {maker.value: "m-new"}}},
    }
    (Path(handler.settings.project_config_root) / "falcone.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(
                    (Path(handler.settings.project_config_root) / "falcone.yaml").read_text()
                ),
                "provider_rotation": handler2_config,
            }
        )
    )
    moved = _route(handler, _payload(change_set_id, uuid4()), _broker())
    assert moved["maker_provider"] == maker.value
    # ... while its model follows the configuration (no deploy needed).
    assert moved["rotation"]["maker"]["model"] == "m-new"


def test_a_fallback_at_the_first_choice_is_recorded_and_then_pinned(tmp_path: Path) -> None:
    handler = _handler(
        tmp_path,
        {
            "enabled": True,
            "enabled_at": ENABLED_AT.isoformat(),
            "roles": {IMPLEMENTER: {"pin": "codex"}},
        },
    )
    change_set_id, run_id = _change_set(handler)
    routed = _route(handler, _payload(change_set_id, run_id), _broker(codex=False))
    # Codex (pinned) is rate limited: Claude makes (recorded fallback), and the checker
    # would be Codex, which is unavailable: today's blocking. The choice is recorded now.
    assert routed["status"] == "BLOCKED"
    record = _recovery(handler, change_set_id)["rotation"]
    assert record["maker"]["provider"] == "claude"
    assert record["maker"]["fallback"] == "codex:provider_rate_limited"
    reserve = _broker(codex=0.1)  # Codex only keeps its verification reserve
    routed = _route(handler, _payload(change_set_id, _add_run(handler, change_set_id)), reserve)
    assert routed["status"] == "ROUTED"
    assert (routed["maker_provider"], routed["checker_provider"]) == ("claude", "codex")
    assert "fallback=codex:provider_rate_limited" in routed["routing_reason"]
    later = _route(handler, _payload(change_set_id, _add_run(handler, change_set_id)), _broker())
    assert later["maker_provider"] == "claude"  # the fallback is the ChangeSet's maker now


def _add_run(handler: MetadataActivityHandler, change_set_id: UUID) -> UUID:
    """Another delivery run (a retry, correction or CI repair) of the same ChangeSet."""
    run_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=run_id,
                campaign_id=None,
                change_set_id=change_set_id,
                workflow_id=f"delivery-{change_set_id}-{run_id}",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )
    return run_id


def _set_rotation(handler: MetadataActivityHandler, rotation: dict[str, Any] | None) -> None:
    path = Path(handler.settings.project_config_root) / "falcone.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest.pop("provider_rotation", None)
    if rotation is not None:
        manifest["provider_rotation"] = rotation
    path.write_text(yaml.safe_dump(manifest))


def test_an_unsuccessful_first_routing_still_pins_the_maker(tmp_path: Path) -> None:
    """Codex review: a first attempt that could not route saved nothing, so the retry (a
    distinct run) met the in-flight rule and the legacy broker chose, then flipped."""
    handler = _handler(
        tmp_path,
        {
            "enabled": True,
            "enabled_at": ENABLED_AT.isoformat(),
            "roles": {IMPLEMENTER: {"pin": "claude"}, SPEC_ARCHITECT: {"pin": "claude"}},
        },
    )
    change_set_id, run_id = _change_set(handler)
    first = _route(handler, _payload(change_set_id, run_id), _broker(False, False))
    assert first["status"] == "RATE_LIMITED"
    assert (
        first["routing_reason"] == "rotation:pinned maker claude unavailable: provider_rate_limited"
    )
    assert _recovery(handler, change_set_id)["rotation"]["maker"]["provider"] == "claude"
    # Retry (distinct run): only Codex is available. Today's broker would choose Codex.
    retry = _route(
        handler,
        _payload(change_set_id, _add_run(handler, change_set_id)),
        _broker(codex=True, claude=False),
    )
    assert retry["status"] == "RATE_LIMITED" and "maker_provider" not in retry
    # Next attempt: both available: the pin holds.
    routed = _route(handler, _payload(change_set_id, _add_run(handler, change_set_id)), _broker())
    assert (routed["maker_provider"], routed["checker_provider"]) == ("claude", "codex")
    # Quota pressure on Claude (only its verification reserve left): today's broker would
    # flip the maker to Codex; the pinned maker waits instead.
    pressed = _route(
        handler,
        _payload(change_set_id, _add_run(handler, change_set_id)),
        _broker(codex=True, claude=0.1),
    )
    assert pressed["status"] == "RATE_LIMITED"
    assert pressed["routing_reason"].startswith("rotation:pinned maker claude unavailable")
    again = _route(handler, _payload(change_set_id, _add_run(handler, change_set_id)), _broker())
    assert again["maker_provider"] == "claude"


MODELS = {
    IMPLEMENTER: {"models": {"claude": "claude-sonnet-5-5", "codex": "gpt-6.1-sol"}},
    CODE_REVIEWER: {"models": {"claude": "claude-opus-5-5"}},
}


def test_models_follow_the_current_configuration_and_return_to_defaults(
    tmp_path: Path,
) -> None:
    enabled = {
        "enabled": True,
        "enabled_at": ENABLED_AT.isoformat(),
        "roles": {**MODELS, IMPLEMENTER: {**MODELS[IMPLEMENTER], "pin": "claude"}},
    }
    handler = _handler(tmp_path, enabled)
    change_set_id, run_id = _change_set(handler)
    routed = _route(handler, _payload(change_set_id, run_id), _broker())
    assert routed["rotation"]["maker"]["model"] == "claude-sonnet-5-5"
    assert routed["rotation"]["checker"]["model"] is None  # Codex reviewer: its default

    def again() -> dict[str, Any]:
        result: dict[str, Any] = _route(
            handler, _payload(change_set_id, _add_run(handler, change_set_id)), _broker()
        )
        return result

    # The override is removed: today's default model, the provider stays pinned.
    _set_rotation(handler, {**enabled, "roles": {IMPLEMENTER: {"pin": "codex"}}})
    removed = again()
    assert removed["maker_provider"] == "claude"
    assert removed["rotation"]["maker"]["model"] is None
    assert "maker=claude/default" in removed["routing_reason"]
    # The whole pilot block is reverted: still pinned (decision 2), models default.
    _set_rotation(handler, None)
    reverted = again()
    assert reverted["maker_provider"] == "claude"
    assert all(reverted["rotation"][key]["model"] is None for key in ("maker", "checker", "spec"))
    # The recorded choice keeps the model chosen at enrolment for the audit.
    assert _recovery(handler, change_set_id)["rotation"]["maker"]["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("off", [{"enabled": False}, None], ids=["disabled", "block-removed"])
def test_after_disabling_fresh_change_sets_route_as_today_and_rotated_ones_stay_pinned(
    tmp_path: Path, off: dict[str, Any] | None
) -> None:
    enabled = {
        "enabled": True,
        "enabled_at": ENABLED_AT.isoformat(),
        "roles": MODELS,
        "ratio": {"codex": 0, "claude": 1},
    }
    handler = _handler(tmp_path, enabled)
    rotated, run_id = _change_set(handler)
    first = _route(handler, _payload(rotated, run_id), _broker())
    assert first["maker_provider"] == "claude"
    _set_rotation(handler, off)
    # The single documented exception: the enrolled ChangeSet keeps its providers (and
    # its rotation keys and routing_reason), with today's default models.
    kept = _route(handler, _payload(rotated, _add_run(handler, rotated)), _broker())
    assert (kept["maker_provider"], kept["checker_provider"]) == ("claude", "codex")
    assert kept["routing_reason"].startswith("rotation:") and ";pinned;" in kept["routing_reason"]
    assert all(kept["rotation"][key]["model"] is None for key in ("maker", "checker", "spec"))
    # Every other ChangeSet routes exactly as origin/main did.
    for (codex, claude), risk in itertools.product(QUOTA_STATES, ("R2", "R3")):
        change_set_id, fresh_run = _change_set(handler)
        routed = _route(handler, _payload(change_set_id, fresh_run, risk), _broker(codex, claude))
        status, maker, checker, spec, reason = TODAY[(codex, claude, risk)]
        assert (routed["status"], routed["routing_reason"]) == (status, reason)
        assert routed.get("maker_provider") == maker and routed.get("spec_provider") == spec
        assert routed.get("checker_provider") == checker and "rotation" not in routed
        assert _recovery(handler, change_set_id) == {}


@pytest.mark.parametrize(
    "in_flight",
    [
        {"created_at": ENABLED_AT - timedelta(seconds=1)},
        {"recovery": {"provider_chain": {"maker": "codex", "checker": "claude"}}},
        {"earlier_runs": 1},
    ],
    ids=["created-before-enabled_at", "recorded-provider-chain", "earlier-delivery-run"],
)
def test_in_flight_change_sets_keep_todays_routing(
    tmp_path: Path, in_flight: dict[str, Any]
) -> None:
    handler = _handler(
        tmp_path,
        {
            "enabled": True,
            "enabled_at": ENABLED_AT.isoformat(),
            "roles": {IMPLEMENTER: {"pin": "claude"}},
        },
    )
    change_set_id, run_id = _change_set(handler, **in_flight)
    before = _recovery(handler, change_set_id)
    routed = _route(handler, _payload(change_set_id, run_id), _broker())
    assert routed["maker_provider"] == "codex"  # today's broker, not the Claude pin
    assert "rotation" not in routed and _recovery(handler, change_set_id) == before


def test_an_invalid_rotation_block_fails_loudly(tmp_path: Path) -> None:
    handler = _handler(tmp_path, {"enabled": True})  # no enabled_at
    change_set_id, run_id = _change_set(handler)
    with pytest.raises(ValueError, match="provider_rotation is invalid"):
        _route(handler, _payload(change_set_id, run_id), _broker())


def test_the_rotated_models_reach_the_agent_requests() -> None:
    payload = {
        "rotation": {
            "maker": {"provider": "claude", "model": "claude-sonnet-5-5"},
            "checker": {"provider": "codex", "model": None},
            "spec": {"provider": "claude", "model": None},
        }
    }
    rotated = MetadataActivityHandler._rotated_model
    assert rotated(payload, "maker") == "claude-sonnet-5-5"
    assert rotated(payload, "checker") is None  # today's default applies
    assert rotated({}, "maker") is None


def test_the_gntik_ui_rotation_proposal_validates() -> None:
    manifest = ProjectManifest.model_validate(
        yaml.safe_load((ROOT / "config/projects/gntik-ui.yaml").read_text(encoding="utf-8"))
    )
    if manifest.provider_rotation is None:
        pytest.skip("the gntik-ui proposal is not in this tree")
    rotation = manifest.provider_rotation
    assert rotation.enabled and rotation.ratio.codex == rotation.ratio.claude
    # The pilot keeps today's default models for every role: only the pairing rotates.
    for role in (IMPLEMENTER, CODE_REVIEWER):
        for provider in (Provider.CLAUDE, Provider.CODEX):
            assert rotation.model_for(role, provider) is None
    assert rotation.enabled_at is not None


def test_a_pinned_spec_author_keeps_todays_routing_at_every_attempt(tmp_path: Path) -> None:
    """Pilot review: a pinned spec author fell back to Codex under Claude quota pressure and
    stayed there after Claude recovered. It now follows today's preference at every routing:
    the pin, the other provider while the pin is unavailable, the pin again once it recovers."""
    handler = _handler(
        tmp_path,
        {
            "enabled": True,
            "enabled_at": ENABLED_AT.isoformat(),
            "roles": {IMPLEMENTER: {"pin": "codex"}, SPEC_ARCHITECT: {"pin": "claude"}},
        },
    )
    change_set_id, run_id = _change_set(handler)
    pressed = _route(handler, _payload(change_set_id, run_id), _broker(codex=True, claude=0.1))
    assert (pressed["maker_provider"], pressed["checker_provider"]) == ("codex", "claude")
    assert pressed["spec_provider"] == "codex"
    recovered = _route(
        handler, _payload(change_set_id, _add_run(handler, change_set_id)), _broker()
    )
    assert recovered["spec_provider"] == "claude"
    assert (recovered["maker_provider"], recovered["checker_provider"]) == ("codex", "claude")
