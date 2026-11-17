"""The evaluation lab: a version of a flow tried on past work before it is put in force.

An evaluation takes runs that happened and goes through each one's work again on another
version of its flow: the same words of the item or the same scope, at the commit the run
worked on. What it is compared with is the run as it went. The runs of an evaluation are
ordinary runs in ordinary sandboxes, on the same subscriptions, and they leave nothing
behind: no pull request, nothing added to the backlog, no item touched. They are started
one after the other, so that an evaluation never takes the subscriptions from the work.

The runs gone through need not be runs of the flow that is tried. A flow can be tried on
the past runs of another flow of the same sort: one in which a model asked over an API
does the work, on the runs of the one in which an agent in a sandbox did it. That puts
the two ways of doing the same work next to each other, on the same words and commit.
"""

from __future__ import annotations

import math
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
from engineering_control.engine import parts, validate
from engineering_control.engine.builtin import FLOW_OF_KIND, kind_of
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import Document, FlowInvalid, parse
from engineering_control.engine.library import traits
from engineering_control.engine.records import FlowRecords, in_force
from engineering_control.flow_library import EVALUATION
from engineering_control.models.providers import BY_KEY as API_BY_KEY
from engineering_control.sandbox.runs import FINISHED, Runs, delete_runs, usage_of

MAX_CASES: Final = 8
# A run that waits for the administrator has done all its work: it can be gone through
# again like one that ended.
REPLAYABLE: Final = (*FINISHED, "waiting")


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _follows(run: SandboxRun, key: str) -> bool:
    """Whether the run is one of that flow. A run of the platform's own flows from before
    runs said which flow they follow is known by its kind."""
    said = ((run.data or {}).get("flow") or {}).get("key")
    return said == key if said else FLOW_OF_KIND.get(run.kind) == key


def sort_of(document: Document) -> str:
    """What a flow works on: an item whose code it changes, or a section it looks at.
    Only runs of flows of one sort can be put next to each other."""
    return "item" if traits(document)["writes"] else "section"


_SORTS: Final = {"item": "works on an item", "section": "is started for a section"}


def _elapsed(steps: list[SandboxStep]) -> int:
    """How long the work took by the clock, in whole minutes: from the first step that
    started to the last that ended, waiting for a sandbox or a model included."""
    started = [step.started_at for step in steps if step.started_at is not None]
    ended = [step.finished_at for step in steps if step.finished_at is not None]
    if not started or not ended:
        return 0
    first = min(value if value.tzinfo else value.replace(tzinfo=UTC) for value in started)
    last = max(value if value.tzinfo else value.replace(tzinfo=UTC) for value in ended)
    return max(0, math.ceil((last - first).total_seconds() / 60))


def measure(
    run: SandboxRun, steps: list[SandboxStep], used: dict[str, Any] | None
) -> dict[str, Any]:
    """What a run came to, in numbers that can be put next to another run's."""
    data = run.data or {}
    result = data.get("result") or {}
    change = data.get("change") or {}
    files = change.get("files") if isinstance(change, dict) else None
    paths = sorted(
        {
            str(entry.get("path"))
            for entry in (files if isinstance(files, list) else [])
            if isinstance(entry, dict) and entry.get("path")
        }
    )
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
        # By the clock, how many steps, how many of them asked a model over an API, and
        # what those questions cost where the accounts say what their models cost.
        "elapsed": _elapsed(steps),
        "steps": len(steps),
        "asked": int((used or {}).get("asked", 0)),
        "cost": round(float((used or {}).get("cost", 0) or 0), 4),
        "counts": result.get("counts") if isinstance(result.get("counts"), dict) else None,
        "files": len(change.get("files") or []) if isinstance(change, dict) else 0,
        "paths": paths[:400],
    }


def in_common(baseline: dict[str, Any] | None, candidate: dict[str, Any] | None) -> int | None:
    """How many files both changes touch, when both changed any: whether the two ways
    went to the same places, which says nothing of which change is the better one."""
    if baseline is None or candidate is None:
        return None
    if not baseline.get("paths") or not candidate.get("paths"):
        return None
    return len(set(baseline["paths"]) & set(candidate["paths"]))


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


def in_batches(document: Document) -> Document:
    """The flow with every question to a model asked in a batch, where the task can be
    and its provider takes batches. A task that brings its model with an agent, and one
    whose provider takes none, is asked at once as it is."""
    raw = document.as_dict()
    for task in raw.get("tasks") or []:
        kind = TASKS.get(str(task.get("task")))
        given = dict(task.get("with") or {})
        provider = API_BY_KEY.get(str(given.get("provider") or ""))
        if (
            kind is not None
            and kind.where == "model"
            and any(param.name == "asked" for param in kind.params)
            and provider is not None
            and provider.batch
        ):
            task["with"] = {**given, "asked": "batch"}
    return parse(raw)


class Evaluations:
    def __init__(self, sessions: sessionmaker[Session], runs: Runs) -> None:
        self.sessions = sessions
        self.runs = runs
        self.flows = FlowRecords(sessions)

    # --- what can be gone through again ---------------------------------------------------

    def _latest(self, key: str) -> Document | None:
        """The flow as it would be tried: its draft, or else the version in force."""
        version = self.flows.draft(key)
        if version is None:
            version = self.flows.version_in_force(key) or 0
        return self.flows.document(key, version)

    def _in_force(self, key: str) -> Document | None:
        return self.flows.document(key, self.flows.version_in_force(key) or 0)

    def sources(self, key: str) -> list[dict[str, Any]]:
        """The flows whose past runs this flow can be tried on: itself, and every other
        flow of the same sort, each with how many of its runs can be gone through."""
        if not self.flows.exists(key):
            raise ProjectNotFound(key)
        mine = self._latest(key)
        if mine is None or mine.part:
            return []
        listed = []
        for other in [key, *(name for name in self.flows.keys() if name != key)]:
            theirs = mine if other == key else self._in_force(other)
            if theirs is None or theirs.part or sort_of(theirs) != sort_of(mine):
                continue
            runs = len(self.candidates(key, of=other, limit=100))
            if other == key or runs:
                listed.append({"key": other, "name": theirs.name, "runs": runs})
        return listed

    def candidates(
        self, key: str, *, of: str | None = None, project: str | None = None, limit: int = 40
    ) -> list[dict[str, Any]]:
        """The runs that can be gone through again, newest first: the ones that got as
        far as checking the repository out. They are runs of the flow itself, or of the
        flow ``of``."""
        if not self.flows.exists(key):
            raise ProjectNotFound(key)
        if of and of != key:
            if not self.flows.exists(of):
                raise ProjectNotFound(of)
            key = of
        kind = kind_of(key)
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
                if (run.data or {}).get("base_sha")
                and run.project_id is not None
                and _follows(run, key)
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
        self,
        key: str,
        *,
        version: int,
        run_ids: list[UUID],
        note: str | None = None,
        of: str | None = None,
        batch: bool = False,
    ) -> dict[str, Any]:
        """Try that version of the flow on those runs' work. The runs are runs of the
        flow itself, or of the flow ``of``, which is of the same sort. With ``batch``
        the models the flow asks are asked in batches, where their providers take them:
        a trial is work nobody waits for, and costs about half that way."""
        if not self.flows.exists(key):
            raise ProjectNotFound(key)
        source = of if of and of != key else key
        if not self.flows.exists(source):
            raise ProjectNotFound(source)
        kind = kind_of(source)
        document = self.flows.document(key, version)
        if document is None:
            raise ProjectNotFound(f"{key} version {version}")
        if document.part:
            raise ProjectInvalid("A part does not run by itself: try a flow that runs it")
        wrong = validate.errors(document)
        if wrong:
            raise ProjectInvalid(f"That version cannot run: {wrong[0]['text']}")
        # What the cases follow is the flow as it runs now, its parts in place: an
        # evaluation keeps what it started with.
        carried, _wrong, _origin = parts.expand(document)
        if batch:
            carried = in_batches(carried)
        if source != key:
            theirs = self._in_force(source)
            if theirs is None:
                raise ProjectInvalid("That flow has never been in force: it has no runs")
            if sort_of(theirs) != sort_of(document):
                raise ProjectInvalid(
                    f"{document.name} {_SORTS[sort_of(document)]} and {theirs.name} "
                    f"{_SORTS[sort_of(theirs)]}: the runs of one say nothing of the other"
                )
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
            current = in_force(session, source)
            if source == key and current is not None and current[1] == document.sha256():
                raise ProjectInvalid(
                    "That version is the one in force: the runs already show how it does"
                )
            past: list[SandboxRun] = []
            for run_id in chosen:
                run = session.get(SandboxRun, run_id)
                if run is None or run.kind != kind or not _follows(run, source):
                    raise ProjectInvalid(
                        "One of those runs is not a run of this flow"
                        if source == key
                        else "One of those runs is not a run of that flow"
                    )
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
                document=carried.as_dict(),
                sha256=document.sha256(),
                of_flow=source if source != key else None,
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

    def remove(self, evaluation_id: UUID) -> dict[str, Any]:
        """Delete an evaluation that is not under way, with its cases and the runs it
        made: those tried a version and left nothing else behind."""
        with self.sessions.begin() as session:
            evaluation = session.get(Evaluation, evaluation_id, with_for_update=True)
            if evaluation is None:
                raise ProjectNotFound(str(evaluation_id))
            if evaluation.state == "running":
                raise ProjectConflict("The evaluation is under way: stop it first")
            cases = session.scalars(
                select(EvaluationCase).where(EvaluationCase.evaluation_id == evaluation.id)
            ).all()
            # Only the runs the evaluation made itself: the candidate's. What it is
            # compared with is past work, which stays.
            made = [
                case.run_id for case in cases if case.arm == "candidate" and case.run_id is not None
            ]
            ended = list(
                session.scalars(
                    select(SandboxRun.id).where(
                        SandboxRun.id.in_(made),
                        SandboxRun.kind == EVALUATION,
                        SandboxRun.state.in_(FINISHED),
                    )
                )
            )
            gone = {
                "id": str(evaluation.id),
                "flow": evaluation.flow_key,
                "candidate_version": evaluation.candidate_version,
                "evaluation": f"{evaluation.flow_key} version {evaluation.candidate_version}",
                "runs": len(ended),
            }
            for case in cases:
                session.delete(case)
            session.flush()
            delete_runs(session, ended)
            session.delete(evaluation)
            return gone

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
            # The flow whose past runs are gone through: the flow tried, or another.
            "of": evaluation.of_flow or evaluation.flow_key,
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
            entry["common"] = in_common(entry["baseline"], entry["candidate"])
            for arm in ("baseline", "candidate"):
                if entry[arm] is not None:
                    entry[arm] = {k: v for k, v in entry[arm].items() if k != "paths"}
        done = sum(
            1
            for entry in listed
            if entry["skipped"]
            or (entry["candidate"] is not None and entry["candidate"]["state"] in FINISHED)
        )
        row = self._row(evaluation, len(listed), done)
        theirs = self._in_force(row["of"])
        row["of_name"] = theirs.name if theirs is not None else row["of"]

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
                "elapsed": sum(m["elapsed"] for m in measured),
                "asked": sum(m["asked"] for m in measured),
                "cost": round(sum(m["cost"] for m in measured), 4),
            }

        row["totals"] = {"baseline": total("baseline"), "candidate": total("candidate")}
        row["verdicts"] = {
            word: sum(1 for entry in listed if entry["verdict"] == word)
            for word in ("better", "same", "worse")
        }
        if full:
            row["items"] = listed
        return row
