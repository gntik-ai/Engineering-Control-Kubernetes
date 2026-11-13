"""The evaluation lab: a version of a flow tried on past work before it is put in force.

An evaluation takes runs that happened and goes through each one's work again on another
version of its flow: the same words of the item or the same scope, at the commit the run
worked on. What it is compared with is the run as it went. The runs of an evaluation are
ordinary runs in ordinary sandboxes, on the same subscriptions, and they leave nothing
behind: no pull request, nothing added to the backlog, no item touched. They are started
one after the other, so that an evaluation never takes the subscriptions from the work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import (
    Evaluation,
    EvaluationCase,
    ProjectRecord,
    SandboxRun,
    SandboxStep,
)
from engineering_control.engine import validate
from engineering_control.engine.builtin import KIND_OF
from engineering_control.engine.document import FlowInvalid, parse
from engineering_control.engine.records import FlowRecords, in_force
from engineering_control.sandbox.runs import FINISHED, Runs, usage_of

MAX_CASES: Final = 8
# A run that waits for the administrator has done all its work: it can be gone through
# again like one that ended.
REPLAYABLE: Final = (*FINISHED, "waiting")


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def measure(
    run: SandboxRun, steps: list[SandboxStep], used: dict[str, Any] | None
) -> dict[str, Any]:
    """What a run came to, in numbers that can be put next to another run's."""
    data = run.data or {}
    result = data.get("result") or {}
    change = data.get("change") or {}
    return {
        "run_id": str(run.id),
        "state": run.state,
        "outcome": run.outcome,
        "detail": run.detail,
        "flow_version": int((data.get("flow") or {}).get("version") or 0),
        # Whether the work came through everything its flow puts before the end: to the
        # administrator's decision, or to the end of a flow that has none.
        "through": bool(data.get("at")) or run.state == "succeeded",
        "rounds": max((int((step.input or {}).get("round") or 1) for step in steps), default=1),
        "path": [
            {"name": step.name, "outcome": step.outcome, "state": step.state} for step in steps
        ][:60],
        "sandboxes": (used or {}).get("sandboxes", 0),
        "minutes": (used or {}).get("minutes", 0),
        "tokens": (used or {}).get("tokens", 0),
        "counts": result.get("counts") if isinstance(result.get("counts"), dict) else None,
        "files": len(change.get("files") or []) if isinstance(change, dict) else 0,
    }


def verdict(baseline: dict[str, Any] | None, candidate: dict[str, Any] | None) -> str | None:
    """How the candidate did next to the run as it went: by whether the work came
    through, then by how many rounds it took. What the work is worth, people judge."""
    if baseline is None or candidate is None or candidate["state"] not in FINISHED:
        return None
    if candidate["through"] != baseline["through"]:
        return "better" if candidate["through"] else "worse"
    if candidate["through"] and candidate["rounds"] != baseline["rounds"]:
        return "better" if candidate["rounds"] < baseline["rounds"] else "worse"
    return "same"


class Evaluations:
    def __init__(self, sessions: sessionmaker[Session], runs: Runs) -> None:
        self.sessions = sessions
        self.runs = runs
        self.flows = FlowRecords(sessions)

    # --- what can be gone through again ---------------------------------------------------

    def candidates(
        self, key: str, *, project: str | None = None, limit: int = 40
    ) -> list[dict[str, Any]]:
        """The runs of the flow that can be gone through again, newest first: the ones
        that got as far as checking the repository out."""
        kind = KIND_OF.get(key)
        if kind is None:
            raise ProjectNotFound(key)
        with self.sessions() as session:
            query = (
                select(SandboxRun)
                .where(SandboxRun.kind == kind, SandboxRun.state.in_(REPLAYABLE))
                .order_by(SandboxRun.created_at.desc())
            )
            if project is not None:
                query = query.join(ProjectRecord, SandboxRun.project_id == ProjectRecord.id).where(
                    ProjectRecord.name == project
                )
            runs = [
                run
                for run in session.scalars(query.limit(300))
                if (run.data or {}).get("base_sha") and run.project_id is not None
            ][: max(1, min(limit, 100))]
            used = usage_of(session, [run.id for run in runs])
            return [
                {
                    "run_id": str(run.id),
                    "title": run.title,
                    "project": (run.data or {}).get("project"),
                    "project_name": (run.data or {}).get("project_name"),
                    "state": run.state,
                    "outcome": run.outcome,
                    "detail": run.detail,
                    "commit": str((run.data or {}).get("base_sha"))[:12],
                    "flow_version": int(((run.data or {}).get("flow") or {}).get("version") or 0),
                    "created_at": _when(run.created_at),
                    "minutes": (used.get(run.id) or {}).get("minutes", 0),
                    "tokens": (used.get(run.id) or {}).get("tokens", 0),
                }
                for run in runs
            ]

    # --- starting and stopping ------------------------------------------------------------

    def start(
        self, key: str, *, version: int, run_ids: list[UUID], note: str | None = None
    ) -> dict[str, Any]:
        """Try that version of the flow on those runs' work."""
        kind = KIND_OF.get(key)
        if kind is None:
            raise ProjectNotFound(key)
        document = self.flows.document(key, version)
        if document is None:
            raise ProjectNotFound(f"{key} version {version}")
        wrong = validate.errors(document)
        if wrong:
            raise ProjectInvalid(f"That version cannot run: {wrong[0]['text']}")
        chosen = list(dict.fromkeys(run_ids))
        if not chosen or len(chosen) > MAX_CASES:
            raise ProjectInvalid(f"Choose between 1 and {MAX_CASES} runs to go through again")
        with self.sessions.begin() as session:
            busy = session.scalar(
                select(Evaluation.id).where(
                    Evaluation.flow_key == key, Evaluation.state == "running"
                )
            )
            if busy is not None:
                raise ProjectConflict("An evaluation of this flow is already under way")
            current = in_force(session, key)
            if current is not None and current[1] == document.sha256():
                raise ProjectInvalid(
                    "That version is the one in force: the runs already show how it does"
                )
            past: list[SandboxRun] = []
            for run_id in chosen:
                run = session.get(SandboxRun, run_id)
                if run is None or run.kind != kind:
                    raise ProjectInvalid("One of those runs is not a run of this flow")
                if run.state not in REPLAYABLE or not (run.data or {}).get("base_sha"):
                    raise ProjectInvalid(f"The run {run.title} cannot be gone through again")
                past.append(run)
            projects = {run.project_id for run in past}
            evaluation = Evaluation(
                flow_key=key,
                candidate_version=version,
                baseline_version=current[0] if current else 0,
                project_id=next(iter(projects)) if len(projects) == 1 else None,
                state="running",
                note=" ".join((note or "").split())[:300] or None,
                document=document.as_dict(),
                sha256=document.sha256(),
            )
            session.add(evaluation)
            session.flush()
            for number, run in enumerate(past, start=1):
                data = run.data or {}
                subject = {
                    "n": number,
                    "title": run.title,
                    "project": data.get("project"),
                    "project_name": data.get("project_name"),
                    "commit": str(data.get("base_sha")),
                    "of_run": str(run.id),
                }
                session.add(
                    EvaluationCase(
                        evaluation_id=evaluation.id, arm="baseline", subject=subject, run_id=run.id
                    )
                )
                session.add(
                    EvaluationCase(
                        evaluation_id=evaluation.id, arm="candidate", subject=subject, run_id=None
                    )
                )
            session.flush()
            found = evaluation.id
        return self.get(found)

    def stop(self, evaluation_id: UUID) -> dict[str, Any]:
        """Start no further case, and stop the one that is running."""
        running: list[UUID] = []
        with self.sessions.begin() as session:
            evaluation = session.get(Evaluation, evaluation_id, with_for_update=True)
            if evaluation is None:
                raise ProjectNotFound(str(evaluation_id))
            if evaluation.state != "running":
                raise ProjectConflict("The evaluation is not under way")
            evaluation.state = "stopped"
            evaluation.finished_at = datetime.now(UTC)
            for case in session.scalars(
                select(EvaluationCase).where(
                    EvaluationCase.evaluation_id == evaluation.id,
                    EvaluationCase.arm == "candidate",
                    EvaluationCase.run_id.is_not(None),
                )
            ):
                run = session.get(SandboxRun, case.run_id)
                if run is not None and run.state not in FINISHED:
                    running.append(run.id)
        for run_id in running:
            self.runs.request_cancel(run_id)
        return self.get(evaluation_id)

    def advance(self) -> int:
        """Start the next case of every evaluation under way, one at a time, and close
        the evaluations that have none left. Returns how many runs it started."""
        started = 0
        with self.sessions() as session:
            under_way = session.scalars(
                select(Evaluation.id).where(Evaluation.state == "running")
            ).all()
        for evaluation_id in under_way:
            started += self._advance(evaluation_id)
        return started

    def _advance(self, evaluation_id: UUID) -> int:
        with self.sessions.begin() as session:
            evaluation = session.get(Evaluation, evaluation_id, with_for_update=True)
            if evaluation is None or evaluation.state != "running":
                return 0
            cases = sorted(
                session.scalars(
                    select(EvaluationCase).where(
                        EvaluationCase.evaluation_id == evaluation.id,
                        EvaluationCase.arm == "candidate",
                    )
                ),
                key=lambda case: int((case.subject or {}).get("n") or 0),
            )
            for case in cases:
                if case.run_id is None:
                    continue
                run = session.get(SandboxRun, case.run_id)
                if run is not None and run.state not in FINISHED:
                    return 0  # one at a time
            waiting = next(
                (
                    case
                    for case in cases
                    if case.run_id is None and not (case.subject or {}).get("skipped")
                ),
                None,
            )
            if waiting is None:
                evaluation.state = "finished"
                evaluation.finished_at = datetime.now(UTC)
                return 0
            case_id = waiting.id
            subject = dict(waiting.subject or {})
            flow = {
                "key": evaluation.flow_key,
                "version": evaluation.candidate_version,
                "sha256": evaluation.sha256,
            }
            document = dict(evaluation.document or {})
        try:
            start = parse(document).start
            run_row = self.runs.start_evaluation(
                UUID(str(subject["of_run"])),
                flow=flow,
                document=document,
                start=start,
                evaluation_id=evaluation_id,
            )
            run_id: UUID | None = UUID(run_row["id"])
            skipped = None
        except (ProjectNotFound, ProjectInvalid, ProjectConflict, FlowInvalid, ValueError) as error:
            run_id, skipped = None, str(error)[:300]
        with self.sessions.begin() as session:
            kept = session.get(EvaluationCase, case_id)
            if kept is not None:
                kept.run_id = run_id
                if skipped is not None:
                    kept.subject = {**(kept.subject or {}), "skipped": skipped}
        return 1 if run_id is not None else 0

    # --- reading --------------------------------------------------------------------------

    @staticmethod
    def _row(evaluation: Evaluation, cases: int, done: int) -> dict[str, Any]:
        return {
            "id": str(evaluation.id),
            "flow": evaluation.flow_key,
            "candidate_version": evaluation.candidate_version,
            "baseline_version": evaluation.baseline_version,
            "sha256": evaluation.sha256,
            "state": evaluation.state,
            "note": evaluation.note,
            "cases": cases,
            "done": done,
            "created_at": _when(evaluation.created_at),
            "finished_at": _when(evaluation.finished_at),
        }

    def listing(self, key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(Evaluation)
                .where(Evaluation.flow_key == key)
                .order_by(Evaluation.created_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
            return [self._detail(session, row, full=False) for row in rows]

    def get(self, evaluation_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            evaluation = session.get(Evaluation, evaluation_id)
            if evaluation is None:
                raise ProjectNotFound(str(evaluation_id))
            return self._detail(session, evaluation, full=True)

    def _detail(self, session: Session, evaluation: Evaluation, *, full: bool) -> dict[str, Any]:
        cases = session.scalars(
            select(EvaluationCase).where(EvaluationCase.evaluation_id == evaluation.id)
        ).all()
        run_ids = [case.run_id for case in cases if case.run_id is not None]
        runs = {
            run.id: run
            for run in session.scalars(select(SandboxRun).where(SandboxRun.id.in_(run_ids)))
        }
        steps: dict[UUID, list[SandboxStep]] = {}
        for step in session.scalars(
            select(SandboxStep)
            .where(SandboxStep.run_id.in_(run_ids))
            .order_by(SandboxStep.run_id, SandboxStep.position)
        ):
            steps.setdefault(step.run_id, []).append(step)
        used = usage_of(session, run_ids)
        by_number: dict[int, dict[str, Any]] = {}
        for case in cases:
            subject = case.subject or {}
            entry = by_number.setdefault(
                int(subject.get("n") or 0),
                {"subject": subject, "baseline": None, "candidate": None, "skipped": None},
            )
            run = runs.get(case.run_id) if case.run_id else None
            if run is not None:
                entry[case.arm] = measure(run, steps.get(run.id, []), used.get(run.id))
            if case.arm == "candidate" and subject.get("skipped"):
                entry["skipped"] = subject["skipped"]
                entry["subject"] = {k: v for k, v in subject.items() if k != "skipped"}
        listed = [by_number[number] for number in sorted(by_number)]
        for entry in listed:
            entry["verdict"] = verdict(entry["baseline"], entry["candidate"])
        done = sum(
            1
            for entry in listed
            if entry["skipped"]
            or (entry["candidate"] is not None and entry["candidate"]["state"] in FINISHED)
        )
        row = self._row(evaluation, len(listed), done)

        def total(arm: str) -> dict[str, Any]:
            measured = [
                entry[arm]
                for entry in listed
                if entry[arm] is not None
                and entry["candidate"] is not None
                and entry["candidate"]["state"] in FINISHED
            ]
            return {
                "cases": len(measured),
                "through": sum(1 for m in measured if m["through"]),
                "rounds": sum(m["rounds"] for m in measured),
                "minutes": sum(m["minutes"] for m in measured),
                "tokens": sum(m["tokens"] for m in measured),
            }

        row["totals"] = {"baseline": total("baseline"), "candidate": total("candidate")}
        row["verdicts"] = {
            word: sum(1 for entry in listed if entry["verdict"] == word)
            for word in ("better", "same", "worse")
        }
        if full:
            row["items"] = listed
        return row
