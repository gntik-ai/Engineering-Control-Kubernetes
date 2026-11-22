"""Agents and skills: who does a task, defined once and named by the tasks that use it.

A task of a flow says what is done and what holds for it: whether it changes the code,
whether passing it counts as the review, where its outcomes lead. An agent says who does
it: standing instructions that go with every task it is given, the subscriptions and
models it works with in a sandbox, the provider and model it is asked at over an API,
how long it may work, and its skills. A skill is a named piece of instructions (how
something is done here) that every agent that has it is given.

A task names an agent in its setting "agent". What the task sets itself stays the
task's; what it leaves open comes from the agent. The rules that hold for every flow do
not ask who does the work: an agent that prefers the subscription that wrote a change is
still not given that change to review.

An agent can be changed without changing a flow. So that what a step was told can be
traced all the same, the step records the agent's name and a hash of what it was told.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.agent_models import problem as model_problem
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import AgentRecord, Skill
from engineering_control.engine.catalogue import (
    AGENT_MINUTES_CEILING,
    ASK_EFFORTS,
    INSTRUCTION_CHARACTERS,
    TASKS,
)
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.engine.records import FlowRecords
from engineering_control.models.providers import API_KEYS, MODEL_NAME
from engineering_control.sandbox import deliver

MAX_AGENTS: Final = 60
MAX_SKILLS: Final = 100
SKILLS_OF_AN_AGENT: Final = 8
SKILL_CHARACTERS: Final = 12_000
DESCRIPTION_CHARACTERS: Final = 600


def _label(name: Any, what: str) -> str:
    label = " ".join(str(name or "").split())[:80]
    if not label:
        raise ProjectInvalid(f"Give the {what} a name")
    return label


def _description(value: Any) -> str | None:
    return " ".join(str(value or "").split())[:DESCRIPTION_CHARACTERS] or None


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def clean_settings(given: Any) -> dict[str, Any]:
    """How an agent works, as the administrator set it, checked and reduced to what is
    known here. Anything left out stays open: the task, or the platform, says it."""
    raw = given if isinstance(given, dict) else {}
    settings: dict[str, Any] = {}
    providers = raw.get("providers") or []
    if not isinstance(providers, list) or any(name not in deliver.PROVIDERS for name in providers):
        raise ProjectInvalid("An agent's subscriptions are Claude and ChatGPT, in the order wanted")
    if len(set(providers)) != len(providers):
        raise ProjectInvalid("An agent names each subscription once")
    if providers:
        settings["providers"] = [str(name) for name in providers]
    models = raw.get("models") or {}
    if not isinstance(models, dict):
        raise ProjectInvalid("An agent's models are given by subscription")
    chosen: dict[str, dict[str, str]] = {}
    for provider, entry in models.items():
        if provider not in deliver.PROVIDERS or not isinstance(entry, dict):
            raise ProjectInvalid("An agent's models are given for Claude and for ChatGPT")
        model, effort = (
            str(entry.get("model") or "").strip(),
            str(entry.get("effort") or "").strip(),
        )
        wrong = model_problem(str(provider), model or None, effort or None)
        if wrong is not None:
            raise ProjectInvalid(wrong)
        kept = {key: value for key, value in (("model", model), ("effort", effort)) if value}
        if kept:
            chosen[str(provider)] = kept
    if chosen:
        settings["models"] = chosen
    minutes = raw.get("minutes")
    if minutes not in (None, ""):
        if (
            not isinstance(minutes, int)
            or isinstance(minutes, bool)
            or not 5 <= minutes <= AGENT_MINUTES_CEILING
        ):
            raise ProjectInvalid(
                f"An agent works between 5 and {AGENT_MINUTES_CEILING} minutes on a task"
            )
        settings["minutes"] = minutes
    api = raw.get("api") or {}
    if not isinstance(api, dict):
        raise ProjectInvalid("How an agent is asked over an API is not given")
    asked: dict[str, str] = {}
    for where, which in (("provider", "model"), ("then_provider", "then_model")):
        provider, model = str(api.get(where) or "").strip(), str(api.get(which) or "").strip()
        if provider in ("", "none"):
            continue
        if provider not in API_KEYS:
            raise ProjectInvalid(f"{provider} is not a provider a model is asked at")
        if not MODEL_NAME.fullmatch(model):
            raise ProjectInvalid("Name the model as its provider writes it, without spaces")
        asked[where], asked[which] = provider, model
    if "then_provider" in asked and "provider" not in asked:
        raise ProjectInvalid("A second provider needs a first")
    effort = str(api.get("effort") or "default")
    if effort not in ASK_EFFORTS:
        raise ProjectInvalid("That is not a level of reasoning")
    if asked:
        settings["api"] = {**asked, **({"effort": effort} if effort != "default" else {})}
    return settings


def told(name: str, instructions: str, skills: list[dict[str, str]]) -> str:
    """What an agent's task is told about who does it, before what the task itself says."""
    parts = [
        f"Who does this work, as the administrator set it: the agent {name}. What follows "
        "holds for every task it is given, beside what the task itself says below.",
        instructions.strip(),
    ]
    for skill in skills:
        parts.append(f"A skill it has, {skill['name']}:\n{skill['text'].strip()}")
    return "\n\n".join(part for part in parts if part)


class Agents:
    def __init__(self, sessions: sessionmaker[Session], flows: FlowRecords | None = None) -> None:
        self.sessions = sessions
        self.flows = flows

    # --- what there is -------------------------------------------------------------------

    @staticmethod
    def _skill(row: Skill) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "name": row.name,
            "description": row.description or "",
            "text": row.text,
            "saved_at": _when(row.updated_at),
            "skill": row.name,
        }

    @staticmethod
    def _agent(row: AgentRecord, names: dict[str, str]) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "name": row.name,
            "description": row.description or "",
            "instructions": row.instructions,
            "settings": dict(row.settings or {}),
            "skills": [skill for skill in row.skills or [] if skill in names],
            "skill_names": [names[skill] for skill in row.skills or [] if skill in names],
            "saved_at": _when(row.updated_at),
            "agent": row.name,
        }

    def _uses(self) -> dict[str, list[dict[str, Any]]]:
        """Which tasks of which flows name each agent."""
        found: dict[str, list[dict[str, Any]]] = {}
        if self.flows is None:
            return found
        for key in self.flows.keys():
            draft = self.flows.draft(key)
            version = draft if draft is not None else self.flows.version_in_force(key)
            document = self.flows.document(key, version or 0)
            if document is None:
                continue
            by_agent: dict[str, list[str]] = {}
            for node in document.nodes:
                named = str(node.params.get("agent") or "")
                if named:
                    by_agent.setdefault(named, []).append(node.id)
            for agent, tasks in by_agent.items():
                found.setdefault(agent, []).append(
                    {"flow": key, "name": document.name, "tasks": tasks, "draft": draft is not None}
                )
        return found

    def platform_roles(self) -> list[dict[str, Any]]:
        """The real sandbox roles and the global flow versions that invoke their contracts."""
        roles: dict[str, dict[str, Any]] = {
            contract.name: {
                "prompt": contract.describe({}),
                "tasks": [
                    {"key": t.key, "name": t.title, "description": t.summary}
                    for t in TASKS.values()
                    if t.account and t.prompt == contract.name
                ],
                "used_by": [],
            }
            for contract in CONTRACTS.values()
        }
        if self.flows:
            for doc, draft in self.flows.global_documents():
                nodes: dict[str, list[str]] = {}
                for node in doc.nodes:
                    task = TASKS.get(node.task)
                    if task and task.prompt in roles:
                        nodes.setdefault(str(task.prompt), []).append(node.id)
                for role, ids in nodes.items():
                    roles[role]["used_by"].append(
                        {
                            "flow": doc.key,
                            "name": doc.name,
                            "tasks": ids,
                            "draft": draft,
                            "custom_wording": bool(doc.prompts.get(role)),
                        }
                    )
        return [role for role in roles.values() if role["tasks"]]

    def listing(self, project: str | None = None) -> dict[str, Any]:
        with self.sessions() as session:
            pid = active_project(session, project).id if project else None
            skills = session.scalars(select(Skill).order_by(func.lower(Skill.name))).all()
            names = {str(skill.id): skill.name for skill in skills}
            agents = session.scalars(
                select(AgentRecord)
                .where(AgentRecord.project_id == pid)
                .order_by(func.lower(AgentRecord.name))
            ).all()
            uses = self._uses() if agents else {}
            held: dict[str, list[str]] = {}
            for agent in agents:
                for skill in agent.skills or []:
                    held.setdefault(str(skill), []).append(agent.name)
            return {
                "roles": self.platform_roles() if project is None else [],
                "agents": [
                    {**self._agent(agent, names), "used_by": uses.get(str(agent.id), [])}
                    for agent in agents
                ],
                "skills": [
                    {**self._skill(skill), "agents": held.get(str(skill.id), [])}
                    for skill in skills
                ],
            }

    def names(self, project: str | None = None) -> list[dict[str, str]]:
        """The agents a task can name, for the designer's list."""
        with self.sessions() as session:
            pid = active_project(session, project).id if project else None
            rows = session.execute(
                select(AgentRecord.id, AgentRecord.name)
                .where((AgentRecord.project_id == pid) | AgentRecord.project_id.is_(None))
                .order_by(func.lower(AgentRecord.name))
            ).all()
            return [{"id": str(agent_id), "name": name} for agent_id, name in rows]

    def get(self, agent_id: Any) -> dict[str, Any] | None:
        """An agent as a task is given it: what it is told, and how it works."""
        try:
            wanted = UUID(str(agent_id))
        except ValueError:
            return None
        with self.sessions() as session:
            row = session.get(AgentRecord, wanted)
            if row is None:
                return None
            ids = [UUID(skill) for skill in row.skills or []]
            found = {
                str(skill.id): skill
                for skill in session.scalars(select(Skill).where(Skill.id.in_(ids))).all()
            }
            skills = [
                {"name": found[skill].name, "text": found[skill].text}
                for skill in row.skills or []
                if skill in found
            ]
            said = told(row.name, row.instructions, skills)
            return {
                "id": str(row.id),
                "name": row.name,
                "settings": dict(row.settings or {}),
                "told": said,
                "sha256": hashlib.sha256(said.encode()).hexdigest(),
            }

    def of(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Several agents at once, by their ids: the ones that are there."""
        found = {agent_id: self.get(agent_id) for agent_id in dict.fromkeys(ids) if agent_id}
        return {agent_id: agent for agent_id, agent in found.items() if agent is not None}

    # --- agents --------------------------------------------------------------------------

    def _checked(self, session: Session, skills: Any) -> list[str]:
        wanted = [str(skill) for skill in skills or []]
        if len(wanted) > SKILLS_OF_AN_AGENT:
            raise ProjectInvalid(f"An agent has {SKILLS_OF_AN_AGENT} skills at most")
        try:
            ids = [UUID(skill) for skill in dict.fromkeys(wanted)]
        except ValueError as error:
            raise ProjectInvalid("That is not a skill") from error
        there = set(session.scalars(select(Skill.id).where(Skill.id.in_(ids))).all())
        if any(skill not in there for skill in ids):
            raise ProjectInvalid("One of the skills is not there any more")
        return [str(skill) for skill in ids]

    @staticmethod
    def _instructions(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ProjectInvalid("Write the agent's instructions: who it is and how it works")
        if len(text) > INSTRUCTION_CHARACTERS:
            raise ProjectInvalid(
                f"The instructions have more than {INSTRUCTION_CHARACTERS:,} characters"
            )
        return text

    def save_agent(
        self,
        agent_id: UUID | None,
        *,
        name: Any,
        description: Any,
        instructions: Any,
        settings: Any,
        skills: Any,
        project: str | None = None,
    ) -> dict[str, Any]:
        label, text, how = (
            _label(name, "agent"),
            self._instructions(instructions),
            clean_settings(settings),
        )
        with self.sessions.begin() as session:
            pid = active_project(session, project, lock=True).id if project else None
            taken = session.scalar(
                select(AgentRecord.id).where(
                    AgentRecord.project_id == pid, func.lower(AgentRecord.name) == label.lower()
                )
            )
            if taken is not None and taken != agent_id:
                raise ProjectConflict(f"There is an agent {label} already")
            held = self._checked(session, skills)
            if agent_id is None:
                if (
                    session.scalar(
                        select(func.count())
                        .select_from(AgentRecord)
                        .where(AgentRecord.project_id == pid)
                    )
                    or 0
                ) >= MAX_AGENTS:
                    raise ProjectConflict(
                        f"There are {MAX_AGENTS} agents already: remove one first"
                    )
                row = AgentRecord(
                    project_id=pid, name=label, instructions=text, settings=how, skills=held
                )
                session.add(row)
            else:
                found = session.get(AgentRecord, agent_id, with_for_update=True)
                if found is None or found.project_id != pid:
                    raise ProjectNotFound(str(agent_id))
                row = found
                row.name, row.instructions, row.settings, row.skills = label, text, how, held
                row.updated_at = datetime.now(UTC)
            row.description = _description(description)
            try:
                session.flush()
            except IntegrityError as error:
                raise ProjectConflict(f"There is an agent {label} already") from error
            names = {str(skill.id): skill.name for skill in session.scalars(select(Skill)).all()}
            return self._agent(row, names)

    def remove_agent(self, agent_id: UUID, project: str | None = None) -> dict[str, Any]:
        """Remove an agent. A flow that names it has to name another before it runs."""
        with self.sessions.begin() as session:
            pid = active_project(session, project, lock=True).id if project else None
            row = session.get(AgentRecord, agent_id)
            if row is None or row.project_id != pid:
                raise ProjectNotFound(str(agent_id))
            gone = self._agent(row, {})
            session.delete(row)
        uses = self._uses().get(str(agent_id), [])
        return {**gone, "still_named_by": [use["name"] for use in uses]}

    # --- skills --------------------------------------------------------------------------

    def save_skill(
        self, skill_id: UUID | None, *, name: Any, description: Any, text: Any
    ) -> dict[str, Any]:
        label, body = _label(name, "skill"), str(text or "").strip()
        if not body:
            raise ProjectInvalid("Write what the skill says")
        if len(body) > SKILL_CHARACTERS:
            raise ProjectInvalid(f"A skill has {SKILL_CHARACTERS:,} characters at most")
        with self.sessions.begin() as session:
            taken = session.scalar(select(Skill.id).where(func.lower(Skill.name) == label.lower()))
            if taken is not None and taken != skill_id:
                raise ProjectConflict(f"There is a skill {label} already")
            if skill_id is None:
                if (session.scalar(select(func.count()).select_from(Skill)) or 0) >= MAX_SKILLS:
                    raise ProjectConflict(
                        f"There are {MAX_SKILLS} skills already: remove one first"
                    )
                row = Skill(name=label, text=body)
                session.add(row)
            else:
                found = session.get(Skill, skill_id, with_for_update=True)
                if found is None:
                    raise ProjectNotFound(str(skill_id))
                row = found
                row.name, row.text = label, body
                row.updated_at = datetime.now(UTC)
            row.description = _description(description)
            session.flush()
            return self._skill(row)

    def remove_skill(self, skill_id: UUID) -> dict[str, Any]:
        """Remove a skill. The agents that had it no longer have it."""
        with self.sessions.begin() as session:
            row = session.get(Skill, skill_id)
            if row is None:
                raise ProjectNotFound(str(skill_id))
            gone = self._skill(row)
            session.delete(row)
            for agent in session.scalars(select(AgentRecord)).all():
                if str(skill_id) in (agent.skills or []):
                    agent.skills = [skill for skill in agent.skills if skill != str(skill_id)]
            return gone
