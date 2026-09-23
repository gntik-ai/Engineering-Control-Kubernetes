"""Read-only NEW-34 diagnostic: why does the spec architect fail with
error_max_structured_output_retries on musematic #1026?

Reads the stored (already redacted) transcript of the spec runs and prints ONLY the
structured-output validation messages and the SHAPE of each attempted output
(field -> length / item count / longest item). Never prints spec content.

  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - < diagnose-new34.py
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from engineering_control.config import get_settings
from engineering_control.db.models import AgentExecution, AgentLogChunk

MARKER = "musematic-44e4bfd3-133c-490e-84e9-b00e28390c5c"


def shape(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    for key, item in value.items():
        if isinstance(item, str):
            result[key] = f"str len={len(item)}" + (f" value={item}" if key == "decision" else "")
        elif isinstance(item, list):
            longest = max((len(str(entry)) for entry in item), default=0)
            result[key] = f"list n={len(item)} longest={longest}"
        else:
            result[key] = type(item).__name__
    return result


engine = create_engine(get_settings().database_url.get_secret_value())
with Session(engine) as session:
    print("executions for campaign:")
    for row in session.scalars(
        select(AgentExecution).where(AgentExecution.workflow_id.contains(MARKER))
    ):
        print(f"  role={row.role} provider={row.provider} status={row.status} "
              f"wf=...{row.workflow_id[-45:]}")
    print("latest 5 executions overall:")
    for row in session.scalars(
        select(AgentExecution).order_by(AgentExecution.started_at.desc()).limit(5)
    ):
        print(f"  {row.started_at} role={row.role} status={row.status} wf=...{row.workflow_id[-45:]}")
    executions = session.scalars(
        select(AgentExecution)
        .where(AgentExecution.workflow_id.contains(MARKER))
        .where(AgentExecution.role == "maker")
        .order_by(AgentExecution.started_at.desc())
        .limit(1)
    ).all()
    for execution in executions:
        print(f"== {execution.workflow_id[-60:]} status={execution.status} "
              f"exit={execution.exit_code} truncated={execution.output_truncated}")
        streams: dict[str, list[int]] = {}
        for chunk in session.scalars(
            select(AgentLogChunk)
            .where(AgentLogChunk.execution_id == execution.id)
            .order_by(AgentLogChunk.sequence)
        ):
            count_bytes = streams.setdefault(chunk.stream, [0, 0])
            count_bytes[0] += 1
            count_bytes[1] += chunk.byte_count
            if count_bytes[0] <= 3:
                for line in chunk.redacted_text.splitlines()[:3]:
                    try:
                        parsed = json.loads(line)
                        layout = (
                            f"json type={parsed.get('type')} keys={sorted(parsed)[:8]}"
                            if isinstance(parsed, dict)
                            else f"json {type(parsed).__name__}"
                        )
                    except ValueError:
                        layout = f"text len={len(line)} starts={line[:12]!r}"
                    print(f"  [{chunk.stream} #{chunk.sequence}] {layout}")
        print("  streams:", {name: f"{n} chunks {b} bytes" for name, (n, b) in streams.items()})
        keywords = re.compile(
            r"schema|structured|validat|minItems|maxLength|maxItems|required|too long|"
            r"empty|decision|BLOCKED|READY",
            re.IGNORECASE,
        )
        tally = {"StructuredOutput: started": 0, "Tool result: failed": 0, "Tool result: completed": 0}
        shown = 0
        for chunk in session.scalars(
            select(AgentLogChunk)
            .where(AgentLogChunk.execution_id == execution.id)
            .order_by(AgentLogChunk.sequence)
        ):
            for line in chunk.redacted_text.splitlines():
                for key in tally:
                    if line.strip() == key:
                        tally[key] += 1
                if line.strip().startswith("Tool result: failed:"):
                    print(f"  [rejection] {line.strip()[:400]}")
                if chunk.stream in {"assistant", "stderr"} and keywords.search(line) and shown < 15:
                    shown += 1
                    print(f"  [{chunk.stream}] {line.strip()[:300]}")
        print("  tally:", tally)
