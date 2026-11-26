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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import review_flows
from engineering_control.dashboard.project_records import ProjectInvalid, active_project
from engineering_control.db.models import ReviewSettings, WorkspaceModelUsage
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
# What measuring a judge is counted under: it has its own sum.
MEASURED: Final = frozenset({"judge_trial", "answer_trial", "compare_trial"})
# How long what a project has spent is remembered before it is added up again.
FRESH_SECONDS: Final = 20.0
_SPENT: dict[UUID, tuple[float, datetime, float]] = {}


@dataclass(frozen=True)
class Settings:
    dollars: float
    since: datetime | None
    answer: tuple[str, str] | None
    judge: tuple[str, str] | None
    # None: the default makers.
    makers: frozenset[str] | None
    rounds: int


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
    )


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


def roles(db: Session, project: UUID) -> dict[str, Any]:
    """The models a run takes unless it is told otherwise, where they can be asked."""
    mine, offered = of(db, project), models(db, project)

    def shown(role: tuple[str, str] | None) -> dict[str, str] | None:
        entry = {"provider": role[0], "model": role[1]} if role else None
        return entry if entry in offered else None

    return {"answer": shown(mine.answer), "judge": shown(mine.judge)}


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
            return {
                "dollars": {
                    "limit": mine.dollars,
                    "spent": round(total, 4),
                    "since": since.isoformat(),
                    "over": total >= mine.dollars,
                },
                "roles": roles(db, project.id),
                "rounds": mine.rounds,
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
    ) -> dict[str, Any]:
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
            for role, what in ((answer, "The model that answers"), (judge, "The judge")):
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
            row.judge_provider, row.judge_model = judge or (None, None)
            row.rounds = rounds
            forget(project.id)
        return self.shown(name)
