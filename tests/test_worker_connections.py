from datetime import UTC, datetime
from pathlib import Path

from engineering_control.db.base import Base
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.operations.connections import ConnectionHealthProjector
from engineering_control.operations.repository import OperationsRepository


def test_every_runtime_worker_identity_projects_its_exact_task_queue(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'connections.db'}"
    operations = OperationsRepository(database_url)
    metadata = SQLMetadataRepository(database_url)
    Base.metadata.create_all(operations.engine)
    identities = {
        "codex-runner": "codex",
        "claude-runner": "claude",
        "browser-runner": "browser",
        "scan-runner": "scan",
        "delivery-runner": "delivery",
        "release-runner": "release",
        "github-runner": "github",
    }
    for identity, kind in identities.items():
        stored = metadata._persist_worker_heartbeat(identity, "worker.example")
        assert stored["worker_kind"] == kind

    ConnectionHealthProjector(operations)._persisted(datetime.now(UTC))
    connections = {item.component: item for item in operations._connections()}
    expected_queues = {
        "codex": "engineering-codex",
        "claude": "engineering-claude",
        "browser": "engineering-browser",
        "scan": "engineering-scan",
        "delivery": "engineering-delivery",
        "release": "engineering-release",
        "github": "engineering-github",
    }
    for kind, queue in expected_queues.items():
        connection = connections[f"worker-{kind}"]
        assert connection.status == "CONNECTED"
        assert connection.logical_target == queue
    controller = connections["worker-controller"]
    assert controller.status == "NOT_INSTALLED"
    assert controller.details == {"replacement_component": "engineering-api"}
