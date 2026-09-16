"""A durable Run exists, but its Temporal start acknowledgement is unconfirmed."""

from uuid import UUID


class RecordedRunStartUnconfirmedError(RuntimeError):
    def __init__(self, workflow_id: str, application_run_id: UUID) -> None:
        super().__init__("Recorded Run start acknowledgement is unconfirmed")
        self.workflow_id = workflow_id
        self.application_run_id = application_run_id
