from datetime import UTC, datetime
from uuid import uuid4

from engineering_control.handoff.store import HandoffStore
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import (
    ModelClass,
    Project,
    Provider,
    RunStatus,
)


def test_rate_limited_handoff_is_complete_and_redacted(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    request = AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="bounded task",
        working_directory=str(root),
        output_schema={"type": "object"},
        writable=True,
        change_set_id=uuid4(),
    )
    now = datetime.now(UTC)
    result = AgentResult(
        run_id=request.run_id,
        provider=Provider.CODEX,
        role="implementer",
        status=RunStatus.RATE_LIMITED,
        started_at=now,
        finished_at=now,
    )
    fake_token = bytes((103, 104, 112, 95)).decode() + "A" * 36
    hashes = HandoffStore(root).persist(
        request,
        result,
        context={"message": fake_token},
        decisions=["bounded decision"],
        source_refs=[{"sha": "a" * 40}],
        diff=f"+token={fake_token}\n",
        test_results={"status": "partial"},
        open_questions=["resume after reset"],
    )
    assert set(hashes) == set(HandoffStore.filenames)
    bundle = root / str(request.run_id) / "handoff"
    assert all((bundle / name).is_file() for name in HandoffStore.filenames)
    assert fake_token not in "".join(
        (bundle / name).read_text(encoding="utf-8") for name in HandoffStore.filenames
    )
