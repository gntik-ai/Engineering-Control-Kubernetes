"""Try another model on questions that already have an answer.

A check of the plan asks a model one question about each record and keeps what it said.
Whether a cheaper model would have said the same is not known until it is asked. A trial
asks it: the same questions, about a sample of the records whose answer on record is
still about the texts as they stand, in an order that is the same for every model, so
that two models are tried on the same records.

What a trial is told is kept apart. Its answers are lines of the trial and of nothing
else: no worklist shows them, no batch is made from them, no count changes by them, and
the answer on record stays the one the check gave. A trial reports how often the model
said the same as the answer on record, how often it said it could not tell, how often
its quoted passages are in the texts, and what a question cost.

The answer on record is another model's, not the truth: a trial measures agreement with
it. Where the administrator approved or set aside an answer, the report says how the
trial did against that.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import (
    cleanup,
    epic_links,
    orphans,
    parent_choice,
    primary_epics,
    review_budget,
    review_settings,
)
from engineering_control.dashboard.project_records import (
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    ChoiceReview,
    ChoiceRun,
    ModelTrialLine,
    ProjectRecord,
    WorkspaceModelUsage,
)
from engineering_control.models.service import Question, Reply

# How many records a trial asks about unless it is told to ask about all of them is in
# the project's settings.
# The trials whose reports are shown.
SHOWN: Final = 20
WORKFLOW: Final = "model_trial"
EMPTY: Final = (
    "No question of this check has an answer on record that a trial can be compared "
    "with. Run the check first."
)


def _order(key: UUID) -> str:
    """Where a record stands in a sample: by a digest of its id, so that the records
    asked about are spread over the project and are the same for every model."""
    return sha256(key.bytes).hexdigest()


def _links(evidence: Any) -> dict[str, str]:
    """What an answer said of each link it judged, for a check that judges several."""
    return {
        str(entry["epic"]): str(entry.get("answer"))
        for entry in (evidence if isinstance(evidence, list) else [])
        if isinstance(entry, dict) and entry.get("side") == "epic" and entry.get("epic")
    }


class _Scope:
    """The records of a check that have an answer on record, asked as the check asks."""

    def __init__(self, db: Session, project: ProjectRecord, base: parent_choice.Kind) -> None:
        self.db, self.project, self.kind = db, project, base
        self.base = base.scope(db, project)
        self._known: dict[UUID, ChoiceReview] | None = None
        self._waiting: list[Any] | None = None

    def known(self) -> dict[UUID, ChoiceReview]:
        if self._known is None:
            self._known = {
                key: review
                for key, review in parent_choice.reviews(
                    self.db, self.project.id, self.kind.name
                ).items()
                if review.state == "answered"
            }
        return self._known

    def children(self) -> list[Any]:
        if self._waiting is None:
            known = self.known()
            found = [child for child in self.base.children() if child.id in known]
            found.sort(key=lambda child: _order(child.id))
            self._waiting = found
        return self._waiting

    def record(self, key: UUID) -> Any | None:
        """That record, when the answer on record is about the texts as they stand: an
        answer about other texts is an answer to another question."""
        review = self.known().get(key)
        child = self.base.record(key) if review is not None else None
        if child is None or review is None:
            return None
        current = self.base.revision(child, self.base.shortlist(child))
        return child if review.revision == current else None

    def refusal(self) -> str | None:
        return self.base.refusal()

    def shortlist(self, child: Any) -> list[dict[str, Any]]:
        return self.base.shortlist(child)

    def revision(self, child: Any, listed: list[dict[str, Any]]) -> str:
        return self.base.revision(child, listed)

    def question(self, run: ChoiceRun, child: Any, listed: list[dict[str, Any]]) -> Question:
        return self.base.question(run, child, listed)

    def read(
        self, data: dict[str, Any], child: Any, listed: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self.base.read(data, child, listed)


class _Trial:
    """A trial of one check. Nothing can be approved from it."""

    workflow = WORKFLOW
    actionable: tuple[str, ...] = ()
    nothing_to_ask = EMPTY
    nothing_waits = EMPTY

    def __init__(self, base: parent_choice.Kind, label: str, noun: tuple[str, str]) -> None:
        self.name = "try_" + base.name
        self.base, self.label, self.noun = base, label, noun

    def scope(self, db: Session, project: ProjectRecord) -> _Scope:
        return _Scope(db, project, self.base)

    def answered(
        self, db: Session, run: ChoiceRun, review: ChoiceReview, child: Any, reply: Reply
    ) -> None:
        """Keep what the model said beside the answer on record, as a line of the trial."""
        record = parent_choice.review_of(db, run.project_id, self.base.name, review.child_id)
        if record is None or record.state != "answered":
            return
        titles = {str(entry.get("id")): str(entry.get("title") or "") for entry in review.shortlist}
        said, on_record = _links(review.evidence), _links(record.evidence)
        named = {
            str(entry.get("epic")): str(entry.get("title") or "")
            for entry in review.evidence or []
            if isinstance(entry, dict) and entry.get("epic")
        }
        differs = [
            {"title": named.get(key, ""), "said": said.get(key, "unclear"), "record": answer}
            for key, answer in on_record.items()
            if said.get(key, "unclear") != answer
        ]
        cost = reply.usage.get("cost")
        db.add(
            ModelTrialLine(
                project_id=run.project_id,
                run_id=run.id,
                kind=self.base.name,
                child_id=review.child_id,
                title=str(getattr(child, "title", "") or "")[:500],
                answer=review.answer or "",
                parent=titles.get(str(review.parent_id), "")[:500] or None,
                reason=review.reason,
                checked=review.checked,
                tokens=review.tokens,
                cost=float(cost) if isinstance(cost, int | float) else None,
                ref_model=record.model,
                ref_answer=record.answer or "",
                ref_parent=titles.get(str(record.parent_id), "")[:500] or None,
                ref_reason=record.reason,
                ref_checked=record.checked,
                ref_tokens=record.tokens,
                ref_decision=record.decision,
                agrees=review.answer == record.answer
                and review.parent_id == record.parent_id
                and not differs,
                settled=parent_choice.settles(
                    self.base,
                    {
                        "answer": review.answer,
                        "checked": review.checked,
                        "confidence": review.confidence,
                        "evidence": review.evidence or [],
                    },
                ),
                links=len(on_record),
                links_same=len(on_record) - len(differs),
                differs=differs or None,
            )
        )


TRIALS: Final[dict[str, _Trial]] = {
    trial.base.name: trial
    for trial in (
        _Trial(
            cleanup.KIND,
            "Whether anything supports a User Story",
            ("User Story", "User Stories"),
        ),
        _Trial(
            epic_links.KIND,
            "The Epics a User Story is also linked to",
            ("User Story", "User Stories"),
        ),
        _Trial(
            primary_epics.KIND, "The primary Epic of a User Story", ("User Story", "User Stories")
        ),
        _Trial(orphans.KIND, "The User Story of an issue", ("issue", "issues")),
    )
}
for _trial in TRIALS.values():
    parent_choice.register(_trial)
BY_NAME: Final = {trial.name: trial for trial in TRIALS.values()}


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _paid(db: Session, project: UUID) -> dict[tuple[str, str], tuple[int, float]]:
    """What the calls of each check cost with each model: how many said what they cost,
    and the sum. From the project's metered calls."""
    paid: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for (usage,) in db.execute(
        select(WorkspaceModelUsage.usage).where(WorkspaceModelUsage.project_id == project)
    ):
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if isinstance(cost, int | float):
            entry = paid[str(usage.get("workflow")), str(usage.get("model"))]
            entry[0] += 1
            entry[1] += float(cost)
    return {key: (int(calls), cost) for key, (calls, cost) in paid.items()}


def _report(
    run: ChoiceRun,
    lines: list[ModelTrialLine],
    paid: dict[tuple[str, str], tuple[int, float]],
    gate: tuple[int, float] = (parent_choice.GATE_SETTLED, parent_choice.GATE_SAME),
) -> dict[str, Any]:
    trial = BY_NAME[run.kind]
    answered = len(lines)
    priced = [line.cost for line in lines if line.cost is not None]
    counted = [line.tokens for line in lines if line.tokens]
    before = [line.ref_tokens for line in lines if line.ref_tokens]
    models = Counter(line.ref_model for line in lines if line.ref_model)
    reference = models.most_common(1)[0][0] if models else None
    calls, cost = paid.get((trial.base.workflow, reference or ""), (0, 0.0))
    table = Counter((line.ref_answer, line.answer) for line in lines)
    decided = {
        decision: {
            "answers": sum(1 for line in lines if line.ref_decision == decision),
            "same": sum(1 for line in lines if line.ref_decision == decision and line.agrees),
        }
        for decision in ("approved", "dismissed")
    }
    links = sum(line.links for line in lines)
    settled = sum(1 for line in lines if line.settled)
    kept_same = sum(1 for line in lines if line.settled and line.agrees)
    return {
        "run": parent_choice.shown_run(run),
        "check": trial.base.name,
        "label": trial.label,
        "noun": list(trial.noun),
        "answered": answered,
        "same": sum(1 for line in lines if line.agrees),
        "same_share": _share(sum(1 for line in lines if line.agrees), answered),
        "links": {"total": links, "same": sum(line.links_same for line in lines)}
        if links
        else None,
        "unclear": sum(1 for line in lines if line.answer == "unclear"),
        "checked": sum(1 for line in lines if line.checked),
        # As a run's first model: the answers it would have kept by itself, how many
        # of those are the same as on record, and whether that passes the gate.
        "settled": settled,
        "settled_same": kept_same,
        "gate": parent_choice.trial_gate(settled, kept_same, *gate),
        "tokens_each": round(sum(counted) / len(counted)) if counted else None,
        "cost": round(sum(priced), 6) if priced else None,
        "cost_each": round(sum(priced) / len(priced), 6) if priced else None,
        "record": {
            "model": reference,
            "models": len(models),
            "unclear": sum(1 for line in lines if line.ref_answer == "unclear"),
            "checked": sum(1 for line in lines if line.ref_checked),
            "tokens_each": round(sum(before) / len(before)) if before else None,
            # What a question of this check cost with that model, over its metered calls.
            "cost_each": round(cost / calls, 6) if calls else None,
        },
        "decided": decided if any(entry["answers"] for entry in decided.values()) else None,
        "table": [
            {"record": on_record, "said": said, "count": count}
            for (on_record, said), count in sorted(
                table.items(), key=lambda entry: (-entry[1], entry[0])
            )
        ],
    }


class ModelTrials:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def overview(self, name: str) -> dict[str, Any]:
        """The checks a model can be tried on, with what a trial would ask and use, and
        the report of each trial so far."""
        with self.sessions() as db:
            project = active_project(db, name)
            placeholder = ChoiceRun(provider="", model="")
            mine = review_settings.of(db, project.id)
            sample = mine.count("trial_sample")
            gate = parent_choice.gate_of(db, project.id)
            checks = []
            for trial in TRIALS.values():
                scope = trial.scope(db, project)
                waiting = scope.children()
                known = scope.known()
                used = [known[child.id].tokens or 0 for child in waiting]
                # What a question holds while it is asked, from the first few of them.
                largest = 0
                for child in waiting[:3]:
                    asked = scope.question(placeholder, child, scope.shortlist(child))
                    largest = max(largest, parent_choice.sizes(asked)[0])
                checks.append(
                    {
                        "check": trial.base.name,
                        "label": trial.label,
                        "noun": list(trial.noun),
                        "answers": len(waiting),
                        "models": sorted({known[child.id].model or "" for child in waiting} - {""}),
                        "sample": min(sample, len(waiting)),
                        # What the answers on record used: the best guide to what another
                        # model uses for the same questions.
                        "likely": sum(used[:sample]),
                        "likely_all": sum(used),
                        "largest": largest,
                        "run": parent_choice.latest_run(db, project.id, trial.name),
                        "first": parent_choice.suggested_first(db, project.id, trial.base.name),
                    }
                )
            runs = db.scalars(
                select(ChoiceRun)
                .where(ChoiceRun.project_id == project.id, ChoiceRun.kind.in_(BY_NAME))
                .order_by(ChoiceRun.created_at.desc(), ChoiceRun.id)
                .limit(SHOWN)
            ).all()
            lines: dict[UUID, list[ModelTrialLine]] = defaultdict(list)
            for line in db.scalars(
                select(ModelTrialLine).where(ModelTrialLine.run_id.in_([run.id for run in runs]))
            ):
                lines[line.run_id].append(line)
            paid = _paid(db, project.id) if runs else {}
            return {
                "checks": checks,
                "trials": [_report(run, lines[run.id], paid, gate) for run in runs],
                "models": review_settings.models(db, project.id),
                "roles": review_settings.roles(db, project.id),
                "budget": review_budget.status(db, project.id),
                "sample": sample,
                "gate": {"settled": gate[0], "same": gate[1]},
            }

    def listing(
        self, name: str, run_id: UUID, show: str, page: int, page_size: int
    ) -> dict[str, Any]:
        """The lines of one trial: what the model said beside the answer on record.
        ``show`` narrows to those where it said otherwise (``differs``) or the same."""
        with self.sessions() as db:
            project = active_project(db, name)
            run = db.get(ChoiceRun, run_id)
            if run is None or run.project_id != project.id or run.kind not in BY_NAME:
                raise ProjectNotFound(str(run_id))
            found = [
                line
                for line in db.scalars(
                    select(ModelTrialLine)
                    .where(ModelTrialLine.run_id == run.id)
                    .order_by(ModelTrialLine.title, ModelTrialLine.id)
                )
                if not show or line.agrees == (show == "same")
            ]
            pages = max(1, (len(found) + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            return {
                "items": [
                    {
                        "id": str(line.child_id),
                        "title": line.title,
                        "same": line.agrees,
                        "said": {
                            "answer": line.answer,
                            "parent": line.parent,
                            "reason": line.reason,
                            "checked": line.checked,
                        },
                        "record": {
                            "answer": line.ref_answer,
                            "parent": line.ref_parent,
                            "reason": line.ref_reason,
                            "checked": line.ref_checked,
                            "model": line.ref_model,
                            "decision": line.ref_decision,
                        },
                        "differs": line.differs or [],
                    }
                    for line in found[(page - 1) * page_size : page * page_size]
                ],
                "total": len(found),
                "page": page,
                "pages": pages,
            }

    def start(
        self,
        name: str,
        check: str,
        provider: str,
        model: str,
        token_cap: int | None,
        everything: bool = False,
    ) -> dict[str, Any]:
        """Begin a trial of a model on a check: a sample of the questions that have an
        answer on record, or all of them."""
        trial = TRIALS.get(check)
        if trial is None:
            raise ProjectInvalid("A model cannot be tried on this check.")
        if not provider or not model:
            raise ProjectInvalid("Choose the model to try.")
        with self.sessions() as db:
            sample = review_settings.of(db, active_project(db, name).id).count("trial_sample")
        taken = 0

        def sampled(_child: Any, _listed: list[dict[str, Any]]) -> bool:
            nonlocal taken
            taken += 1
            return everything or taken <= sample

        return parent_choice.start(
            self.sessions, name, trial.name, provider, model, token_cap, True, sampled
        )

    def control(self, name: str, check: str, run_id: UUID, action: str) -> dict[str, Any]:
        trial = TRIALS.get(check)
        if trial is None:
            raise ProjectNotFound(check)
        return parent_choice.control(self.sessions, name, trial.name, run_id, action)
