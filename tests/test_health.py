"""What every process says about itself, the state of the work as numbers, and the rules
written on them."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from engineering_control.config import Settings
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import ProjectRecords
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogSync,
    ProjectRecord,
    ProviderAccount,
    SandboxRun,
    SandboxStep,
    SupervisorIncident,
)
from engineering_control.health import Health, listen, sample
from engineering_control.platform_metrics import DESCRIPTIONS, platform_metrics, unavailable

ROOT = Path(__file__).parents[1]
MONITORING = ROOT / "deploy" / "monitoring"
BASE = ROOT / "deploy" / "base"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_loop_that_stops_turning_is_said_to_stand() -> None:
    clock = Clock()
    health = Health("launcher", clock=clock)
    assert health.stalled() == []
    health.expect("pass", within=60)
    clock.now += 59
    assert health.stalled() == []
    health.beat("pass")
    clock.now += 61
    assert health.stalled() == ["pass"]
    health.beat("pass")
    assert health.stalled() == []
    # A loop nobody named is not watched, and one that is forgotten no longer is.
    health.beat("other")
    assert "other" not in "\n".join(health.metrics())
    health.forget("pass")
    clock.now += 1000
    assert health.stalled() == []


def test_the_numbers_are_in_the_text_format_and_never_more_than_numbers() -> None:
    clock = Clock()
    health = Health("egress", clock=clock)
    health.expect("pass", within=10)
    health.count("egress_refused")
    health.count("egress_refused")
    health.count("egress_tunnels", host='a"b\nc')
    clock.now += 5
    lines = health.metrics()
    assert 'engineering_process_uptime_seconds{process="egress"} 5.0' in lines
    assert 'engineering_loop_age_seconds{loop="pass",process="egress"} 5.0' in lines
    assert 'engineering_loop_stalled{loop="pass",process="egress"} 0' in lines
    assert 'engineering_egress_refused_total{process="egress"} 2' in lines
    assert 'engineering_egress_tunnels_total{host="a\\"b c",process="egress"} 1' in lines
    with pytest.raises(ValueError):
        sample("not a name", 1)


async def _get(port: int, request: bytes) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    answer = (await reader.read()).decode()
    writer.close()
    head, _, body = answer.partition("\r\n\r\n")
    return int(head.split()[1]), body


@pytest.mark.asyncio
async def test_the_listener_answers_health_and_numbers_and_nothing_else() -> None:
    clock = Clock()
    health = Health("chat", clock=clock)
    health.expect("news", within=30)

    async def extra() -> list[str]:
        return [sample("engineering_database_up", 1)]

    server = await listen(health, "127.0.0.1", 0, extra=extra)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _get(port, b"GET /healthz HTTP/1.1\r\n\r\n") == (200, "ok\n")
        status, body = await _get(port, b"GET /metrics?x=1 HTTP/1.1\r\n\r\n")
        assert status == 200 and "engineering_database_up 1" in body
        assert 'engineering_loop_stalled{loop="news",process="chat"} 0' in body
        clock.now += 31
        assert await _get(port, b"GET /healthz HTTP/1.1\r\n\r\n") == (503, "stalled: news\n")
        assert (await _get(port, b"GET /anything HTTP/1.1\r\n\r\n"))[0] == 404
        assert (await _get(port, b"POST /healthz HTTP/1.1\r\n\r\n"))[0] == 404
        assert (await _get(port, b"\xff\xfe nonsense\r\n\r\n"))[0] == 404
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_numbers_that_cannot_be_read_do_not_take_the_process_own_away() -> None:
    health = Health("launcher")

    async def broken() -> list[str]:
        raise RuntimeError("the database is away")

    server = await listen(health, "127.0.0.1", 0, extra=broken)
    port = server.sockets[0].getsockname()[1]
    try:
        status, body = await _get(port, b"GET /metrics HTTP/1.1\r\n\r\n")
        assert status == 200 and "engineering_process_uptime_seconds" in body
    finally:
        server.close()
        await server.wait_closed()


def _site(tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'numbers.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    ProjectRecords(sessions).sync_registry()
    SectionRecords(sessions).sync_core()
    return sessions


def _value(lines: list[str], name: str, **labels: str) -> float:
    wanted = sample(name, 0, **labels).rsplit(" ", 1)[0]
    (line,) = [line for line in lines if line.rsplit(" ", 1)[0] == wanted]
    return float(line.rsplit(" ", 1)[1])


def test_the_state_of_the_work_is_read_from_the_records(tmp_path: Path) -> None:
    sessions = _site(tmp_path)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    empty = platform_metrics(sessions, now=now)
    assert _value(empty, "engineering_database_up") == 1
    assert _value(empty, "engineering_run_oldest_queued_seconds") == 0
    assert _value(empty, "engineering_runs", state="queued") == 0

    BacklogItems(sessions).add("falcone", title="One thing", section=None)
    with sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone").one()
        for state, minutes in (("queued", 40), ("queued", 5), ("running", 3), ("waiting", 2)):
            session.add(
                SandboxRun(
                    kind="flow",
                    state=state,
                    title=state,
                    project_id=project.id,
                    created_at=now - timedelta(minutes=minutes),
                )
            )
        for state, minutes in (("failed", 10), ("failed", 20), ("succeeded", 30), ("failed", 90)):
            session.add(
                SandboxRun(
                    kind="flow",
                    state=state,
                    title=state,
                    project_id=project.id,
                    finished_at=now - timedelta(minutes=minutes),
                )
            )
        # A turn of the supervisor is a conversation, not work.
        turn = SandboxRun(kind="supervisor_turn", state="running", title="A turn")
        session.add(turn)
        session.flush()
        session.add(SandboxStep(run_id=turn.id, position=0, name="turn", state="running"))
        session.add(ProviderAccount(name="one", provider="claude", state="ready"))
        session.add(ProviderAccount(name="two", provider="chatgpt", state="sign_in_required"))
        session.add(
            ProviderAccount(name="rest", provider="claude", state="sign_in_required", paused=True)
        )
        session.add(SupervisorIncident(kind="run_failed", ref="a", state="open", summary="x"))
        session.add(SupervisorIncident(kind="run_failed", ref="b", state="handled", summary="x"))
        session.add(BacklogSync(project_id=project.id, synced_at=now, error="GitHub said no"))
    lines = platform_metrics(sessions, now=now)
    assert _value(lines, "engineering_runs", state="queued") == 2
    assert _value(lines, "engineering_runs", state="running") == 1
    assert _value(lines, "engineering_runs", state="waiting") == 1
    assert _value(lines, "engineering_run_oldest_queued_seconds") == 2400
    assert _value(lines, "engineering_runs_ended_last_hour", state="failed") == 2
    assert _value(lines, "engineering_runs_ended_last_hour", state="succeeded") == 1
    assert _value(lines, "engineering_steps_running") == 1
    assert _value(lines, "engineering_accounts", state="ready") == 1
    assert _value(lines, "engineering_accounts", state="sign_in_required") == 1
    assert _value(lines, "engineering_incidents", state="open") == 1
    assert _value(lines, "engineering_backlog_open_items") == 1
    assert _value(lines, "engineering_backlog_sync_failing") == 1
    # Every number is described, and nothing is said that is not described.
    named = {line.split("{")[0].split(" ")[0] for line in lines if not line.startswith("#")}
    assert named == set(DESCRIPTIONS)
    assert unavailable()[-1] == "engineering_database_up 0"


def _exported() -> set[str]:
    """Every name a process of the platform can say."""
    source = "\n".join(
        path.read_text() for path in (ROOT / "src" / "engineering_control").rglob("*.py")
    )
    counters = {f"engineering_{name}_total" for name in re.findall(r'\.count\("([a-z_]+)"', source)}
    return (
        set(DESCRIPTIONS)
        | counters
        | {
            "engineering_process_uptime_seconds",
            "engineering_loop_age_seconds",
            "engineering_loop_stalled",
            "engineering_dashboard_persistence_failures_total",
            "engineering_dashboard_persistence_failure_active",
        }
    )


def test_every_rule_is_written_on_numbers_the_platform_says_and_names_its_runbook() -> None:
    document = yaml.safe_load((MONITORING / "rules.yaml").read_text())
    assert document["kind"] == "PrometheusRule"
    guide = (ROOT / "deploy" / "README.md").read_text()
    anchors = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).strip().replace(" ", "-")
        for heading in re.findall(r"^#{2,4} (.+)$", guide, re.M)
    }
    exported = _exported()
    seen = set()
    for group in document["spec"]["groups"]:
        for rule in group["rules"]:
            assert rule["alert"].startswith("Engineering") and rule["alert"] not in seen
            seen.add(rule["alert"])
            used = set(re.findall(r"engineering_[a-z_]+", rule["expr"]))
            assert used and used <= exported, (rule["alert"], used - exported)
            assert rule["labels"]["severity"] in {"critical", "warning", "info"}
            assert rule["annotations"]["summary"]
            path, _, anchor = rule["annotations"]["runbook"].partition("#")
            assert path == "deploy/README.md" and anchor in anchors, rule["alert"]
    assert len(seen) >= 12


def test_every_process_with_a_loop_is_restarted_when_it_stands_and_all_say_their_numbers() -> None:
    documents = []
    for path in BASE.glob("*.yaml"):
        documents.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
    deployments = {
        d["metadata"]["name"].removeprefix("engineering-control-"): d
        for d in documents
        if d["kind"] == "Deployment"
    }
    assert set(deployments) == {"console", "launcher", "egress", "tools", "gateway", "chat"}
    settings = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap" and "ENGINEERING_DASHBOARD_HOST" in d["data"]
    )
    assert settings["ENGINEERING_HEALTH_HOST"] == "0.0.0.0"  # noqa: S104 - a pod's own
    port = Settings().health_port
    for name, deployment in deployments.items():
        template = deployment["spec"]["template"]
        (container,) = template["spec"]["containers"]
        ports = {p["name"]: p["containerPort"] for p in container.get("ports", [])}
        assert container.get("livenessProbe"), name
        if name in {"tools", "gateway"}:
            # Probed on the port they serve on: they have no loop that could stand.
            assert "health" not in ports
            continue
        assert ports["health"] == port, name
        assert template["metadata"]["annotations"] == {
            "prometheus.io/scrape": "true",
            "prometheus.io/port": str(port),
            "prometheus.io/path": "/metrics",
        }, name
        if name in {"launcher", "chat"}:
            # No listener of their own: the health port is how the kubelet knows.
            assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "health"}
            assert container["startupProbe"]["httpGet"] == {"path": "/healthz", "port": "health"}
    # The health port is in no Service: nothing outside a pod's own network needs it.
    for document in documents:
        if document["kind"] == "Service":
            assert all(p.get("targetPort") != "health" for p in document["spec"]["ports"])
    monitor = yaml.safe_load((MONITORING / "pod-monitor.yaml").read_text())
    assert monitor["spec"]["podMetricsEndpoints"][0]["port"] == "health"
    policy = yaml.safe_load((MONITORING / "network-policy.yaml").read_text())
    assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": port}]
    listed = yaml.safe_load((MONITORING / "kustomization.yaml").read_text())["resources"]
    assert set(listed) == {p.name for p in MONITORING.glob("*.yaml")} - {"kustomization.yaml"}
