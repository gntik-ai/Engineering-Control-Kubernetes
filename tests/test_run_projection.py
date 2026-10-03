from datetime import UTC, datetime, timedelta

from engineering_control.run_projection import classify_run_projection


def test_run_projection_distinguishes_terminal_wait_orphan_and_active() -> None:
    now = datetime.now(UTC)
    assert (
        classify_run_projection(
            status="BLOCKED",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(minutes=1),
            now=now,
        )
        == "TERMINAL"
    )
    assert (
        classify_run_projection(
            status="AWAITING_APPROVAL",
            started_at=now - timedelta(hours=2),
            finished_at=None,
            updated_at=now - timedelta(hours=1),
            now=now,
        )
        == "DURABLE_WAIT"
    )
    assert (
        classify_run_projection(
            status="QUEUED",
            started_at=None,
            finished_at=None,
            updated_at=now - timedelta(minutes=16),
            now=now,
        )
        == "ORPHANED_START_INTENT"
    )
    assert (
        classify_run_projection(
            status="RUNNING",
            started_at=now - timedelta(minutes=1),
            finished_at=None,
            updated_at=now,
            now=now,
        )
        == "ACTIVE_REQUIRES_TEMPORAL_CORRELATION"
    )
