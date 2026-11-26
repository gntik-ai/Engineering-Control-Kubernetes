"""What a project's reviews are set to, in one place (J2 of docs/design/judged-reviews.md).

A limit in US dollars a month, beside the one in tokens: a token of one model costs up
to a hundred times a token of another, so tokens alone do not limit what is spent. The
models that answer and that judge, which a run takes unless it is told otherwise. The
makers a project's texts may be sent to. And how often an answer goes back when its
judge says otherwise.

A project without a record of its own has the defaults. The limit in dollars counts what
the project's reviews have cost since the month began or since the limit was first put
in force, whichever is later: what was spent before there was a limit is not held
against it. Measurements of judges have a sum of their own and are not counted here.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import review_flows
from engineering_control.dashboard.project_records import ProjectInvalid, active_project
from engineering_control.db.models import ReviewSettings, WorkspaceImport, WorkspaceModelUsage
from engineering_control.models.providers import family_of, maker

# Decided on 26 November 2026.
DOLLARS: Final = 3.0
# An answer does not go back unless the project's settings say so. Measured on Falcone on
# 26 November 2026: answers the two agreed on at once were right 38 times in 45, and those
# they agreed on only after one was told what the other chose, 4 times in 7.
ROUNDS: Final = 0
# The makers a project's texts are sent to unless its settings say otherwise, beside the
# makers of the decision models; and the makers nothing is sent to without the
# administrator's word.
MAKERS: Final = frozenset({"anthropic", "openai", "google"})
AT_YOUR_WORD: Final = frozenset(
    {
        "alibaba",
        "baidu",
        "bytedance",
        "bytedance-seed",
        "deepseek",
        "glm",
        "inclusionai",
        "kimi",
        "minimax",
        "moonshotai",
        "qwen",
        "stepfun",
        "tencent",
        "xiaomi",
        "z-ai",
        "zhipu",
    }
)
# How far what two models agree on goes without the administrator: it stays in the
# worklist, it is put into a batch to approve, or it is applied where the project's
# record of judged samples allows.
ALONE: Final = ("off", "propose", "apply")
# What measuring a judge is counted under: it has its own sum.
MEASURED: Final = frozenset({"judge_trial", "answer_trial", "compare_trial"})
# How long what a project has spent is remembered before it is added up again.
FRESH_SECONDS: Final = 20.0
_SPENT: dict[UUID, tuple[float, datetime, float]] = {}


@dataclass(frozen=True)
class Number:
    """A number the reviews work to: what a project has unless its settings say
    otherwise, the least and the most it can be set to, and what it is for."""

    default: float
    low: float
    high: float
    unit: str
    what: str
    whole: bool = True


# In the order they are shown. A share is kept in hundredths, as it is read.
NUMBERS: Final[dict[str, Number]] = {
    "run_tokens": Number(
        1_000_000,
        10_000,
        5_000_000,
        "tokens",
        "The most one run of a check uses before it stops.",
    ),
    "evidence_tokens": Number(
        12_000,
        1_000,
        2_000_000,
        "tokens",
        "The most one run of Review evidence uses.",
    ),
    "candidates": Number(12, 3, 30, "", "User Stories shown as candidates for an issue."),
    "epic_margin": Number(
        1.5,
        1.05,
        5,
        "times",
        "How far the best Epic must lead the second for a User Story to count as clear.",
        whole=False,
    ),
    "add_error": Number(
        5,
        1,
        20,
        "%",
        "The share of wrong lines a batch that adds or corrects may have in its sample "
        "before it is withdrawn, and the error the scorer may make where it adds.",
        whole=False,
    ),
    "remove_error": Number(
        2,
        0.5,
        20,
        "%",
        "The same for a batch that removes or closes.",
        whole=False,
    ),
    "sample_least": Number(5, 1, 50, "lines", "The fewest lines in the sample of a batch."),
    "sample_most": Number(20, 1, 100, "lines", "The most lines in the sample of a batch."),
    "undo_days": Number(30, 1, 365, "days", "How long an approved batch can be undone."),
    "gate_settled": Number(
        30,
        1,
        500,
        "answers",
        "What a model must settle by itself in its trial on a check to be asked first.",
    ),
    "gate_same": Number(
        90,
        50,
        100,
        "%",
        "The share of those in which it must say the same as the answer on record.",
        whole=False,
    ),
    "trial_sample": Number(100, 10, 500, "records", "Records a trial of another model asks about."),
    "judge_sample": Number(
        60, 10, 200, "pairs", "Right pairs, and as many wrong ones, a measurement of a judge asks."
    ),
    "alone_from": Number(
        40,
        1,
        500,
        "lines",
        "Lines of batches you must have judged before what is agreed is applied without you.",
    ),
    "alone_sample": Number(
        5,
        1,
        50,
        "%",
        "The share of what was applied without you that is put before you to judge.",
        whole=False,
    ),
    "alone_waiting": Number(
        20,
        1,
        200,
        "lines",
        "Lines to judge that may wait before applying without you pauses.",
    ),
    "doubt_dollars": Number(
        1,
        0,
        100,
        "$",
        "What the third model, asked where two differ, may cost a month.",
        whole=False,
    ),
    "measure_dollars": Number(
        3,
        0,
        100,
        "$",
        "What all measurements of judges of the project may cost together.",
        whole=False,
    ),
}


@dataclass(frozen=True)
class Settings:
    dollars: float
    since: datetime | None
    answer: tuple[str, str] | None
    judge: tuple[str, str] | None
    # None: the default makers.
    makers: frozenset[str] | None
    rounds: int
    # The model asked about what the two did not agree on; none for no third model.
    doubt: tuple[str, str] | None = None
    # Where there is no judge: whether the model whose trial on a check agreed is asked
    # before the model that answers.
    cheaper_first: bool = True
    # The numbers the project set for itself; the others are the defaults.
    own: Mapping[str, float] = field(default_factory=dict)
    # How far each kind of change goes without the administrator, by the kind of batch.
    alone: Mapping[str, str] = field(default_factory=dict)
    # Which checks run by themselves, by the check: as records arrive, or at night.
    runs: Mapping[str, str] = field(default_factory=dict)

    def goes(self, kind: str) -> str:
        """ "off", "propose" or "apply" for that kind of batch; proposing unless set."""
        said = self.alone.get(kind)
        return said if said in ALONE else "propose"

    def number(self, key: str) -> float:
        known = NUMBERS[key]
        value = self.own.get(key)
        if not isinstance(value, int | float) or not known.low <= value <= known.high:
            return known.default
        return float(value)

    def count(self, key: str) -> int:
        return int(self.number(key))

    def share(self, key: str) -> float:
        return self.number(key) / 100


def of(db: Session, project: UUID) -> Settings:
    row = db.scalar(select(ReviewSettings).where(ReviewSettings.project_id == project))
    if row is None:
        return Settings(DOLLARS, None, None, None, None, ROUNDS)
    return Settings(
        row.dollar_limit,
        row.dollars_from,
        (row.answer_provider or "", row.answer_model) if row.answer_model else None,
        (row.judge_provider or "", row.judge_model) if row.judge_model else None,
        frozenset(row.makers) if row.makers is not None else None,
        row.rounds,
        (row.doubt_provider or "", row.doubt_model) if row.doubt_model else None,
        row.cheaper_first,
        dict(row.numbers or {}),
        dict(row.alone or {}),
        dict(row.runs or {}),
    )


def number(db: Session, project: UUID, key: str) -> float:
    return of(db, project).number(key)


def default_makers(db: Session) -> frozenset[str]:
    """Anthropic, OpenAI and Google, and whoever makes a decision model an account
    offers, without the makers that wait for the administrator's word."""
    decided = {
        maker(entry["provider"], entry["model"]) for entry in review_flows.decision_models(db)
    }
    return frozenset((MAKERS | decided) - AT_YOUR_WORD)


def makers(db: Session, project: UUID) -> frozenset[str]:
    mine = of(db, project).makers
    return mine if mine is not None else default_makers(db)


def permits(db: Session, project: UUID, provider: str, model: str) -> bool:
    return maker(provider, model) in makers(db, project)


def models(db: Session, project: UUID) -> list[dict[str, str]]:
    """The models a review of this project can ask: those a review can ask at all, of
    the makers its texts may be sent to."""
    allowed = makers(db, project)
    return [
        entry
        for entry in review_flows.model_choices(db)
        if maker(entry["provider"], entry["model"]) in allowed
    ]


def judge_of(db: Session, project: UUID, answered: tuple[str, str]) -> tuple[str, str] | None:
    """The judge the project's settings name, where it can judge an answer of this
    model: one that can be asked, of a maker the project's texts may be sent to, and of
    another family than the model that answered. None otherwise."""
    judge = of(db, project).judge
    if judge is None or family_of(*judge) == family_of(*answered):
        return None
    if {"provider": judge[0], "model": judge[1]} not in models(db, project):
        return None
    return judge


def roles(db: Session, project: UUID) -> dict[str, Any]:
    """The models a run takes unless it is told otherwise, where they can be asked."""
    mine, offered = of(db, project), models(db, project)

    def shown(role: tuple[str, str] | None) -> dict[str, str] | None:
        entry = {"provider": role[0], "model": role[1]} if role else None
        return entry if entry in offered else None

    return {
        "answer": shown(mine.answer),
        "judge": shown(mine.judge),
        "third": shown(mine.doubt),
    }


def _from(mine: Settings, now: datetime) -> datetime:
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    since = mine.since
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return max(month, since) if since is not None else month


def spent(db: Session, project: UUID, *, fresh: bool = False) -> tuple[float, datetime]:
    """What the project's reviews have cost since the limit began to count, in US
    dollars, and since when. From the calls whose account says what its model costs."""
    now = datetime.now(UTC)
    since = _from(of(db, project), now)
    kept = _SPENT.get(project)
    if not fresh and kept is not None and kept[1] == since and kept[0] > time.monotonic():
        return kept[2], since
    total = 0.0
    for (usage,) in db.execute(
        select(WorkspaceModelUsage.usage).where(
            WorkspaceModelUsage.project_id == project, WorkspaceModelUsage.recorded_at >= since
        )
    ):
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if isinstance(cost, int | float) and usage.get("workflow") not in MEASURED:
            total += float(cost)
    _SPENT[project] = (time.monotonic() + FRESH_SECONDS, since, total)
    return total, since


def over(db: Session, project: UUID) -> str | None:
    """Why no review of the project may spend now, when its limit in dollars is reached."""
    mine = of(db, project)
    total, since = spent(db, project)
    if total < mine.dollars:
        return None
    return (
        f"This project's reviews may cost ${mine.dollars:,.2f} a month and have cost "
        f"${total:,.2f} since {since:%-d %B}. Raise the limit under Reviews, Settings."
    )


def forget(project: UUID) -> None:
    _SPENT.pop(project, None)


# What a review's calls are counted under, as it is called.
WORKFLOWS: Final = {
    "orphan_parent": "Issues with no User Story",
    "primary_epic": "Primary Epics",
    "cleanup_check": "Clean-up check",
    "epic_links": "Extra Epic links",
    "epic_page": "Epic overviews",
    "model_trial": "Trial of another model",
    "judge_trial": "Measurement of judges",
    "answer_trial": "Measurement of a second answer",
    "compare_trial": "Measurement of a comparison",
    "automatic_review": "Automatic planning reviews",
    "automatic_review_screen": "Automatic planning reviews: first look",
    "automatic_review_linking": "Automatic planning reviews: links",
    "repository_review": "Discovery",
    "project_planning": "Discovery and planning",
    "epic_description_validation": "Epic descriptions",
    "connection_review": "Connections between projects",
    "execution_planning": "Execution order",
    "issue_linking": "Issues and their User Stories: coverage",
    "project_coverage": "Discovery: coverage",
    "supervisor_advice": "Supervisor: advice on the plan",
}


def _label(workflow: str) -> str:
    """What a workflow is called; a judge's calls are counted under its name with
    ``_judge``."""
    judge = workflow.endswith("_judge")
    base = workflow[: -len("_judge")] if judge else workflow
    said = WORKFLOWS.get(base) or base.replace("_", " ").capitalize()
    return f"{said}: judge" if judge else said


def costs(db: Session, project: UUID, since: datetime) -> list[dict[str, Any]]:
    """What the project's reviews have used since then, for each workflow and model:
    calls, tokens and US dollars, the dearest first. A call whose account does not say
    what its model costs is counted without a price."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for (usage,) in db.execute(
        select(WorkspaceModelUsage.usage).where(
            WorkspaceModelUsage.project_id == project, WorkspaceModelUsage.recorded_at >= since
        )
    ):
        if not isinstance(usage, dict):
            continue
        workflow = str(usage.get("workflow") or "")
        model = str(usage.get("model") or usage.get("requested_model") or "")
        row = rows.setdefault(
            (workflow, model),
            {
                "workflow": workflow,
                "label": _label(workflow),
                "model": model,
                "calls": 0,
                "tokens": 0,
                "dollars": 0.0,
                "unpriced": 0,
                # Measurements of judges have their own sum.
                "counted": workflow not in MEASURED,
            },
        )
        row["calls"] += 1
        tokens = usage.get("tokens")
        if not isinstance(tokens, int) and all(
            isinstance(usage.get(key), int) for key in ("input", "output")
        ):
            tokens = usage["input"] + usage["output"]
        row["tokens"] += tokens if isinstance(tokens, int) else 0
        cost = usage.get("cost")
        if isinstance(cost, int | float):
            row["dollars"] += float(cost)
        else:
            row["unpriced"] += 1
    found = sorted(rows.values(), key=lambda row: (-row["dollars"], -row["tokens"], row["label"]))
    for row in found:
        row["dollars"] = round(row["dollars"], 6)
    return found


class ReviewSettingsService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def shown(self, name: str) -> dict[str, Any]:
        with self.sessions() as db:
            project = active_project(db, name)
            mine = of(db, project.id)
            total, since = spent(db, project.id, fresh=True)
            allowed = makers(db, project.id)
            offered: dict[str, int] = {}
            for entry in review_flows.model_choices(db):
                made_by = maker(entry["provider"], entry["model"])
                offered[made_by] = offered.get(made_by, 0) + 1
            for entry in review_flows.decision_models(db):
                offered.setdefault(maker(entry["provider"], entry["model"]), 0)
            defaults = default_makers(db)
            from engineering_control.dashboard import auto_checks, parent_choice, reorg
            from engineering_control.dashboard.planning_analysis import selection

            # The model those reviews ask: the one chosen for planning, else Discovery's.
            chosen = selection(project, db.get(WorkspaceImport, project.id))
            planning = (chosen[0] or "", chosen[1] or "")
            return {
                "dollars": {
                    "limit": mine.dollars,
                    "spent": round(total, 4),
                    "since": since.isoformat(),
                    "over": total >= mine.dollars,
                },
                "roles": roles(db, project.id),
                # Whether the judge is asked in the planning reviews, Discovery, the
                # execution order and the check of a new Epic: it has to be of another
                # maker than the model those ask. None where there is no such model.
                "planning_judged": (judge_of(db, project.id, planning) is not None)
                if planning[1]
                else None,
                "rounds": mine.rounds,
                "cheaper_first": mine.cheaper_first,
                "numbers": [
                    {
                        "key": key,
                        "value": mine.number(key),
                        "default": known.default,
                        "low": known.low,
                        "high": known.high,
                        "unit": known.unit,
                        "what": known.what,
                        "whole": known.whole,
                    }
                    for key, known in NUMBERS.items()
                ],
                # What each workflow and model has used since the limit began to count,
                # and what a run of each check would ask and is likely to cost.
                "costs": costs(db, project.id, since),
                "checks": parent_choice.estimates(db, project),
                # How far each kind of change goes without the administrator, with the
                # record that decides whether applying is in force.
                "alone": [reorg.alone_standing(db, project.id, kind) for kind in reorg.ALONE],
                # Which checks run by themselves, and what each waits for now.
                "runs": [
                    {
                        **auto_checks.standing(db, project, kind, datetime.now(UTC)),
                        "label": label,
                    }
                    for kind, label in parent_choice.CHECKS.items()
                    if kind in parent_choice.KINDS
                ],
                "makers": [
                    {
                        "maker": made_by,
                        "models": count,
                        "allowed": made_by in allowed,
                        "by_default": made_by in defaults,
                        "at_your_word": made_by in AT_YOUR_WORD,
                    }
                    for made_by, count in sorted(offered.items())
                ],
                "own_makers": mine.makers is not None,
                "models": models(db, project.id),
            }

    def save(
        self,
        name: str,
        dollars: float,
        answer: tuple[str, str] | None,
        judge: tuple[str, str] | None,
        allowed: list[str] | None,
        rounds: int,
        cheaper_first: bool = True,
        numbers: Mapping[str, float] | None = None,
        third: tuple[str, str] | None = None,
        alone: Mapping[str, str] | None = None,
        runs: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Keep what the project's reviews are set to. ``numbers`` none leaves the
        numbers as they are; given, a number that is its default is not kept, so that
        the project follows the default when that changes."""
        own: dict[str, float] | None = None
        if numbers is not None:
            own = {}
            for key, value in numbers.items():
                known = NUMBERS.get(key)
                if known is None:
                    raise ProjectInvalid("That is not a number of the reviews.")
                if not known.low <= value <= known.high or (known.whole and value != int(value)):
                    raise ProjectInvalid(
                        f"“{known.what}” takes {'a whole number' if known.whole else 'a number'} "
                        f"from {known.low:g} to {known.high:g}."
                    )
                if value != known.default:
                    own[key] = float(value)
            least = own.get("sample_least", NUMBERS["sample_least"].default)
            if least > own.get("sample_most", NUMBERS["sample_most"].default):
                raise ProjectInvalid(
                    "The fewest lines in a batch's sample cannot be more than the most."
                )
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            row = db.scalar(select(ReviewSettings).where(ReviewSettings.project_id == project.id))
            if row is None:
                # The limit counts from now for a project that had none.
                row = ReviewSettings(project_id=project.id, dollars_from=datetime.now(UTC))
                db.add(row)
            row.makers = sorted(set(allowed)) if allowed is not None else None
            db.flush()
            offered = models(db, project.id)
            for role, what in (
                (answer, "The model that answers"),
                (judge, "The judge"),
                (third, "The third model"),
            ):
                if role and {"provider": role[0], "model": role[1]} not in offered:
                    raise ProjectInvalid(
                        f"{what} has to be a model of an account that can be asked and of "
                        "a maker this project's texts may be sent to."
                    )
            if answer and judge and family_of(*answer) == family_of(*judge):
                raise ProjectInvalid(
                    "The judge is of the family that answers. Choose a model of another maker."
                )
            row.dollar_limit = dollars
            row.answer_provider, row.answer_model = answer or (None, None)
            if third is not None and (judge is None or third in (answer, judge)):
                raise ProjectInvalid(
                    "The third model is asked where the judge differs: it needs a judge, and "
                    "has to be another model than the one that answers and the judge."
                )
            row.judge_provider, row.judge_model = judge or (None, None)
            row.doubt_provider, row.doubt_model = third or (None, None)
            row.rounds = rounds
            row.cheaper_first = cheaper_first
            if own is not None:
                row.numbers = own or None
            if alone is not None:
                from engineering_control.dashboard import reorg

                if any(k not in reorg.ALONE or v not in ALONE for k, v in alone.items()):
                    raise ProjectInvalid("That is not a way a change can go on.")
                row.alone = {k: v for k, v in alone.items() if v != "propose"} or None
            if runs is not None:
                from engineering_control.dashboard import auto_checks, parent_choice

                if any(
                    k not in parent_choice.CHECKS or v not in auto_checks.MODES
                    for k, v in runs.items()
                ):
                    raise ProjectInvalid("That is not a check, or not a way it can run.")
                row.runs = {k: v for k, v in runs.items() if v != "off"} or None
            forget(project.id)
        return self.shown(name)
