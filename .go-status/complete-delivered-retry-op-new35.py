"""One-off NEW-35 remediation: retry operation 16d2b319 delivered its signal to the
#1026 loop at 09:58:40Z but, before NEW-35, stayed STARTED against that long-lived loop,
so every later backlog-retry of the loop was deduplicated onto it and sent nothing.

Marks that one operation COMPLETED through the deployed OperationsRepository.finish --
what the fixed command bus now does right after delivering a control signal.
Refuses to act unless it is still a STARTED retry for that loop.

  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - < complete-delivered-retry-op-new35.py
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from engineering_control.config import get_settings
from engineering_control.db.models import OperationRequest
from engineering_control.operations.repository import OperationsRepository

OPERATION_ID = UUID("16d2b319-ac98-4618-88ce-b85a1365a50e")
LOOP_WORKFLOW_ID = (
    "backlog-loop-musematic-44e4bfd3-133c-490e-84e9-b00e28390c5c-"
    "ae4214cc-2026-4896-a330-b1daf7d8d252"
)

repository = OperationsRepository(get_settings().database_url.get_secret_value())
with repository.sessions() as session:
    row = session.get(OperationRequest, OPERATION_ID)
    before = (row.action, row.status, row.workflow_id) if row else None
print("before:", before)
if before != ("retry", "STARTED", LOOP_WORKFLOW_ID):
    raise SystemExit("unexpected operation state; nothing done")
asyncio.run(
    repository.finish(OPERATION_ID, status="COMPLETED", workflow_id=LOOP_WORKFLOW_ID, run_id="")
)
with repository.sessions() as session:
    row = session.get(OperationRequest, OPERATION_ID)
    print("after:", (row.action, row.status) if row else None)
