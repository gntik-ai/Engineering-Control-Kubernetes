"""The supervisor's definition: prompt, skills, tools and subscription, in versions.

The supervisor is defined like any agent, and the administrator edits the definition in the
console. Every save is a new version; the highest is in force, and an older one is brought
back by saving it again. The tools are a fixed catalogue provided by the platform: the
definition switches each on or off and says whether what it does is carried out at once or
waits for the administrator. What a tool may do at all is decided where it runs
(``supervisor/tools.py``), not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import ProjectInvalid, ProjectNotFound
from engineering_control.db.models import ProviderAccount, SupervisorDefinition

PROVIDERS: Final = ("claude", "chatgpt")
MODES: Final = ("automatic", "confirm")
MAX_PROMPT: Final = 20_000
MAX_SKILLS: Final = 20
MAX_SKILL_BODY: Final = 8_000
SKILL_KEY: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){0,5}$")
MODEL_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,59}$")

_OBJECT: Final[dict[str, Any]] = {"type": "object", "additionalProperties": False}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {**_OBJECT, "properties": properties, "required": list(required)}


_PROJECT: Final = {
    "type": "string",
    "description": "The project's name, as list_projects gives it.",
}
_ITEM: Final = {"type": "string", "description": "The item's id, as list_items gives it."}
_QUEUE: Final = {"type": "string", "description": "The queue's id, as list_queues gives it."}
_REASON: Final = {
    "type": "string",
    "description": "Why, in one or two sentences the administrator can check.",
}


@dataclass(frozen=True)
class Tool:
    """One operation the supervisor may call. ``kind`` groups the tools by what they can
    do; ``modes`` are the ways the administrator may have it confirmed."""

    name: str
    kind: str
    title: str
    description: str
    schema: dict[str, Any]
    default_mode: str = "automatic"
    modes: tuple[str, ...] = ("automatic",)

    def listed(self) -> dict[str, Any]:
        """The tool as the client is told about it."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {"readOnlyHint": self.kind == "read", "openWorldHint": False},
        }


CATALOGUE: Final[tuple[Tool, ...]] = (
    Tool(
        "list_projects",
        "read",
        "List the projects",
        "The projects of the platform: name, repository, how many open items each has.",
        _schema({}),
    ),
    Tool(
        "get_project",
        "read",
        "Read a project",
        "One project: its sections with their flows, and the routing brief that says which "
        "work belongs in which section. Read it before routing or proposing work.",
        _schema({"project": _PROJECT}, ("project",)),
    ),
    Tool(
        "list_items",
        "read",
        "List backlog items",
        "Backlog items, most recently active first. Narrow by project, by section (a "
        "section key, or 'none' for items not routed yet), by state (proposed, accepted, "
        "skipped, done) or by words of the title or an issue number.",
        _schema(
            {
                "project": _PROJECT,
                "section": {"type": "string"},
                "state": {"type": "string", "enum": ["proposed", "accepted", "skipped", "done"]},
                "search": {"type": "string"},
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    ),
    Tool(
        "get_item",
        "read",
        "Read a backlog item",
        "One backlog item: where it is routed and why, its latest run, and, when it is a "
        "GitHub issue, the issue's text and labels as they are now.",
        _schema({"item": _ITEM}, ("item",)),
    ),
    Tool(
        "list_runs",
        "read",
        "List runs",
        "Runs of work, newest first: what each is for, its state and outcome. Narrow by "
        "state (queued, running, waiting, succeeded, failed, cancelled) or by project.",
        _schema(
            {
                "state": {
                    "type": "string",
                    "enum": ["queued", "running", "waiting", "succeeded", "failed", "cancelled"],
                },
                "project": _PROJECT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    ),
    Tool(
        "get_run",
        "read",
        "Read a run",
        "One run with its steps: what each step was, how it ended and what it said. Name a "
        "step's position in log_of_step to read the end of what that step printed.",
        _schema(
            {
                "run": {"type": "string", "description": "The run's id."},
                "log_of_step": {"type": "integer", "minimum": 1},
            },
            ("run",),
        ),
    ),
    Tool(
        "list_queues",
        "read",
        "List the queues",
        "The queues of work: name, project, state (draft, running, paused, finished) and "
        "how many of their items wait, run, are done or failed.",
        _schema({"project": _PROJECT}),
    ),
    Tool(
        "get_queue",
        "read",
        "Read a queue",
        "One queue with its items in order: what each is, where it stands and its run.",
        _schema({"queue": _QUEUE}, ("queue",)),
    ),
    Tool(
        "list_accounts",
        "read",
        "List the subscription accounts",
        "The subscription accounts work runs on: provider, state, and until when one is "
        "limited. Never a credential.",
        _schema({}),
    ),
    Tool(
        "get_inbox",
        "read",
        "Read the inbox",
        "What waits for the administrator right now: runs to decide on, accounts to sign "
        "in, proposals of yours, items without a section.",
        _schema({}),
    ),
    Tool(
        "propose_items",
        "plan",
        "Propose backlog items",
        "Propose new backlog items for a project. Each appears in the backlog as proposed "
        "by you, for the administrator to accept, edit or reject. Give each a section when "
        "one fits (read the project first) and always a reason.",
        _schema(
            {
                "project": _PROJECT,
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": _schema(
                        {
                            "title": {"type": "string", "maxLength": 300},
                            "section": {
                                "type": "string",
                                "description": "A section key of the project; leave out "
                                "when no section's guide fits.",
                            },
                            "reason": _REASON,
                        },
                        ("title", "reason"),
                    ),
                },
            },
            ("project", "items"),
        ),
        modes=MODES,
    ),
    Tool(
        "route_item",
        "plan",
        "Route an item to a section",
        "Say which section of its project an item belongs to, from the project's routing "
        "brief. One section per item. When no guide fits, do not call this: say so. Give "
        "your confidence honestly: anything but 'high' is shown to the administrator for "
        "confirmation instead of applied, and so is a change to a choice they made.",
        _schema(
            {
                "item": _ITEM,
                "section": {
                    "type": "string",
                    "description": "A section key of the item's project.",
                },
                "reason": _REASON,
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            ("item", "section", "reason", "confidence"),
        ),
        modes=MODES,
    ),
    Tool(
        "propose_queue",
        "plan",
        "Propose a queue",
        "Propose an order to work on items of one project in. The queue is a draft the "
        "administrator starts, reorders or discards; nothing runs until they start it. "
        "Give the reason for the order as a whole and, for each item, why it stands where "
        "it does: what it depends on, its risk, its priority.",
        _schema(
            {
                "project": _PROJECT,
                "name": {"type": "string", "maxLength": 120},
                "reason": _REASON,
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "In the order they are to be worked on.",
                    "items": _schema({"item": _ITEM, "reason": _REASON}, ("item", "reason")),
                },
            },
            ("project", "name", "reason", "items"),
        ),
        modes=MODES,
    ),
    Tool(
        "reorder_queue",
        "plan",
        "Reorder a queue",
        "Put the items of a queue that still wait into another order. Name every waiting "
        "item once. A queue the administrator made, or one that is running, is reordered "
        "only with their confirmation.",
        _schema(
            {
                "queue": _QUEUE,
                "items": {"type": "array", "minItems": 1, "maxItems": 200, "items": _ITEM},
                "reason": _REASON,
            },
            ("queue", "items", "reason"),
        ),
        modes=MODES,
    ),
    Tool(
        "start_queue",
        "operate",
        "Start or resume a queue",
        "Start a draft queue, or resume a paused one: its items then run in order. Only "
        "when the administrator asked for it.",
        _schema({"queue": _QUEUE, "reason": _REASON}, ("queue", "reason")),
        default_mode="confirm",
        modes=MODES,
    ),
    Tool(
        "pause_queue",
        "operate",
        "Pause a queue",
        "Stop a running queue from starting further items. What is running runs to its "
        "end; nothing is lost and the queue can be resumed.",
        _schema({"queue": _QUEUE, "reason": _REASON}, ("queue", "reason")),
        modes=MODES,
    ),
    Tool(
        "start_item",
        "operate",
        "Start work on an item",
        "Start the flow of the item's section for it: a run begins. Only for an item the "
        "administrator asked you to start.",
        _schema({"item": _ITEM, "reason": _REASON}, ("item", "reason")),
        default_mode="confirm",
        modes=MODES,
    ),
)
TOOLS: Final = {tool.name: tool for tool in CATALOGUE}

DEFAULT_PROMPT: Final = """\
You are the supervisor of Engineering Control, a platform that plans and runs engineering \
work on software projects for one person: the administrator. You talk with them in the \
console and in chat. You are their colleague for planning: you look things up, say what you \
found, propose what to do next, and carry out what they ask within what your tools allow.

How you work
- Look before you speak. Use your tools to read the projects, the backlog and the runs \
instead of guessing or relying on what was said earlier in the conversation; things change.
- Be brief and concrete. Lead with the answer. Name items by their title and issue number, \
runs by what they are for. Use short lists when there are several things; no preamble, no \
recap of what was asked.
- Say what you did and what you only propose. An action is done only when its tool said so. \
When a tool says an action waits for the administrator, tell them it is in their inbox and \
do not try it again.
- When you are not sure, say so and ask. One clear question is better than a guess.
- Never invent an item, a run, a result or a number. If a tool fails, say that it failed.

What you decide and what you do not
- You plan: you propose backlog items, say which section an item belongs to, and propose \
the order to work in as a queue.
- You start work, or a queue, only when the administrator asked for it. You may pause a \
queue yourself when its items keep failing; say why.
- The administrator decides what is published. You never approve, merge, deploy or publish \
anything, and you never touch accounts or credentials.
"""

DEFAULT_SKILLS: Final[tuple[dict[str, Any], ...]] = (
    {
        "key": "build-the-backlog",
        "name": "Build the backlog",
        "description": "Turning a conversation, open issues or a goal into backlog items.",
        "body": (
            "When the administrator wants to know what to work on, or describes a goal:\n"
            "1. Read the project (get_project) and what is already in its backlog "
            "(list_items). Do not propose what is already there.\n"
            "2. Propose items that are each one piece of work with a clear outcome. A title "
            "says what will be different when it is done, not how.\n"
            "3. Give each item the section its work belongs to, by the routing brief, and "
            "a reason that says where it came from (what the administrator said, which "
            "issue, which run).\n"
            "4. Propose a few good items rather than many vague ones, then tell the "
            "administrator what you proposed and what you left out and why."
        ),
        "enabled": True,
    },
    {
        "key": "route-an-item",
        "name": "Route an item to a section",
        "description": "Choosing the section of a project an item belongs to.",
        "body": (
            "Routing decides which flow will run for an item, so it follows the project's "
            "routing brief and nothing else.\n"
            "1. Read the item (get_item): for an issue, its text and labels, not only the "
            "title.\n"
            "2. Read the project's routing brief (get_project). Choose the one section "
            "whose guide fits the work. Sections are chosen by their guide, never by "
            "whether they have a flow.\n"
            "3. If no guide fits, do not route: say which sections you considered and "
            "why none fits, and ask. Never choose the nearest one.\n"
            "4. If the fitting section has no flow, route to it anyway and say that the "
            "section is not configured. Never send the work elsewhere instead.\n"
            "5. Confidence is 'high' only when the guide names this kind of work plainly. "
            "When two sections could claim it, it is 'medium' at most: say which two.\n"
            "When asked to route many items, work through them in batches and report "
            "counts per section, then the ones you could not place."
        ),
        "enabled": True,
    },
    {
        "key": "order-a-queue",
        "name": "Order a queue",
        "description": "Proposing the order in which to work on items.",
        "body": (
            "When asked what to work on next, or to queue up work:\n"
            "1. Read the open items of the project (list_items) and what is already "
            "queued or running (list_queues, list_runs). Leave out items that are in a "
            "queue, have a run under way, or have no section whose flow can run.\n"
            "2. Order by, first, what other items depend on; then what is small and safe "
            "before what is large or risky; then what the administrator said matters "
            "most. When you cannot tell, keep the backlog's order and say so.\n"
            "3. Propose the queue (propose_queue) with one reason for the whole order "
            "and one per item. Keep it short: a queue of five items that will run is "
            "better than one of thirty.\n"
            "4. Tell the administrator the order and why. The queue is a draft: they "
            "start it, reorder it or discard it. Start it yourself only if they asked."
        ),
        "enabled": True,
    },
    {
        "key": "report-on-work",
        "name": "Report on work",
        "description": "Answering what is running, what failed and what waits.",
        "body": (
            "When asked how things stand:\n"
            "1. Read the inbox (get_inbox) and the recent runs (list_runs).\n"
            "2. Lead with what needs the administrator: runs waiting for their decision, "
            "accounts that need a sign-in, then failures.\n"
            "3. For a failed run, read it (get_run) and say in one sentence which step "
            "failed and why, in plain words; read the end of that step's output only when "
            "the outcome does not explain it.\n"
            "4. Do not list what is fine unless asked."
        ),
        "enabled": True,
    },
)


def default_tools() -> dict[str, dict[str, Any]]:
    return {tool.name: {"enabled": True, "mode": tool.default_mode} for tool in CATALOGUE}


def merged_tools(stored: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The stored choices over the catalogue: a tool the definition does not mention yet
    (it was added to the platform later) has its default."""
    merged = default_tools()
    for name, choice in (stored or {}).items():
        tool = TOOLS.get(name)
        if tool is None or not isinstance(choice, dict):
            continue
        mode = choice.get("mode")
        merged[name] = {
            "enabled": bool(choice.get("enabled", True)),
            "mode": mode if mode in tool.modes else tool.default_mode,
        }
    return merged


def clean_skills(skills: Any) -> list[dict[str, Any]]:
    if not isinstance(skills, list) or len(skills) > MAX_SKILLS:
        raise ProjectInvalid(f"A supervisor has at most {MAX_SKILLS} skills")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict):
            raise ProjectInvalid("A skill has a name, a description and instructions")
        name = " ".join(str(skill.get("name") or "").split())
        if not 1 <= len(name) <= 80:
            raise ProjectInvalid("A skill's name has between 1 and 80 characters")
        key = str(skill.get("key") or "").strip() or "-".join(
            re.sub(r"[^a-z0-9 ]", "", name.lower()).split()[:6]
        )
        if not SKILL_KEY.fullmatch(key) or key in seen:
            raise ProjectInvalid(f"Two skills cannot share the name {name}")
        seen.add(key)
        description = " ".join(str(skill.get("description") or "").split())
        body = str(skill.get("body") or "").strip()
        if len(description) > 300:
            raise ProjectInvalid("A skill's description has at most 300 characters")
        if not 1 <= len(body) <= MAX_SKILL_BODY:
            raise ProjectInvalid(
                f"A skill's instructions have between 1 and {MAX_SKILL_BODY} characters"
            )
        cleaned.append(
            {
                "key": key,
                "name": name,
                "description": description,
                "body": body,
                "enabled": bool(skill.get("enabled", True)),
            }
        )
    return cleaned


def clean_tools(tools: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(tools, dict):
        raise ProjectInvalid("The tools are given by name")
    for name, choice in tools.items():
        tool = TOOLS.get(name)
        if tool is None:
            raise ProjectInvalid(f"There is no tool {name}")
        if not isinstance(choice, dict) or choice.get("mode", tool.default_mode) not in tool.modes:
            raise ProjectInvalid(f"{tool.title} cannot be confirmed that way")
    return merged_tools(tools)


def clean_providers(providers: Any) -> list[str]:
    if (
        not isinstance(providers, list)
        or not providers
        or len(set(providers)) != len(providers)
        or any(name not in PROVIDERS for name in providers)
    ):
        raise ProjectInvalid("Name the providers the supervisor may work on, most wanted first")
    return [str(name) for name in providers]


def clean_models(models: Any) -> dict[str, str]:
    if not isinstance(models, dict):
        raise ProjectInvalid("The models are given by provider")
    cleaned: dict[str, str] = {}
    for provider, name in models.items():
        if provider not in PROVIDERS:
            raise ProjectInvalid(f"There is no provider {provider}")
        text = str(name or "").strip()
        if text and not MODEL_NAME.fullmatch(text):
            raise ProjectInvalid(f"{text} is not a model's name")
        if text:
            cleaned[provider] = text
    return cleaned


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _row(definition: SupervisorDefinition | None) -> dict[str, Any]:
    if definition is None:
        # Nothing saved yet: the platform's own definition, as version 0.
        return {
            "version": 0,
            "prompt": DEFAULT_PROMPT,
            "skills": [dict(skill) for skill in DEFAULT_SKILLS],
            "tools": default_tools(),
            "providers": list(PROVIDERS),
            "account_id": None,
            "exclusive": False,
            "models": {},
            "note": "The platform's own definition",
            "saved_at": None,
        }
    return {
        "version": definition.version,
        "prompt": definition.prompt,
        "skills": [dict(skill) for skill in definition.skills or []],
        "tools": merged_tools(definition.tools),
        "providers": [name for name in definition.providers or [] if name in PROVIDERS]
        or list(PROVIDERS),
        "account_id": str(definition.account_id) if definition.account_id else None,
        "exclusive": bool(definition.exclusive),
        "models": dict(definition.models or {}),
        "note": definition.note,
        "saved_at": _when(definition.created_at),
    }


def catalogue() -> list[dict[str, Any]]:
    """The tools as the settings page shows them."""
    return [
        {
            "name": tool.name,
            "kind": tool.kind,
            "title": tool.title,
            "description": tool.description,
            "modes": list(tool.modes),
            "default_mode": tool.default_mode,
        }
        for tool in CATALOGUE
    ]


class Definitions:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _latest(session: Session) -> SupervisorDefinition | None:
        return session.scalar(
            select(SupervisorDefinition).order_by(SupervisorDefinition.version.desc()).limit(1)
        )

    def current(self) -> dict[str, Any]:
        with self.sessions() as session:
            return _row(self._latest(session))

    def get(self, version: int) -> dict[str, Any]:
        """That version, or the one in force when it is not there any more."""
        if version < 1:
            return _row(None)
        with self.sessions() as session:
            found = session.scalar(
                select(SupervisorDefinition).where(SupervisorDefinition.version == version)
            )
            return _row(found or self._latest(session))

    def versions(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self.sessions() as session:
            found = session.scalars(
                select(SupervisorDefinition)
                .order_by(SupervisorDefinition.version.desc())
                .limit(limit)
            ).all()
            return [
                {"version": item.version, "note": item.note, "saved_at": _when(item.created_at)}
                for item in found
            ]

    def reserved_account(self) -> UUID | None:
        """The account only the supervisor works on, when the definition says so."""
        with self.sessions() as session:
            latest = self._latest(session)
            if latest is None or not latest.exclusive:
                return None
            return latest.account_id

    def save(
        self,
        *,
        prompt: str,
        skills: Any,
        tools: Any,
        providers: Any,
        account_id: UUID | None,
        exclusive: bool,
        models: Any,
        note: str | None,
    ) -> dict[str, Any]:
        text = prompt.strip()
        if not 1 <= len(text) <= MAX_PROMPT:
            raise ProjectInvalid(f"The prompt has between 1 and {MAX_PROMPT} characters")
        cleaned_skills = clean_skills(skills)
        cleaned_tools = clean_tools(tools)
        cleaned_providers = clean_providers(providers)
        cleaned_models = clean_models(models)
        with self.sessions.begin() as session:
            if account_id is not None:
                account = session.get(ProviderAccount, account_id)
                if account is None or account.archived_at is not None:
                    raise ProjectNotFound(str(account_id))
                if account.provider not in cleaned_providers:
                    raise ProjectInvalid(
                        f"{account.name} is a {account.provider} account, which is not among "
                        "the providers chosen"
                    )
            elif exclusive:
                raise ProjectInvalid("Choose the account that is to be the supervisor's own")
            last = session.scalar(select(func.max(SupervisorDefinition.version))) or 0
            definition = SupervisorDefinition(
                version=last + 1,
                prompt=text,
                skills=cleaned_skills,
                tools=cleaned_tools,
                providers=cleaned_providers,
                account_id=account_id,
                exclusive=bool(exclusive and account_id is not None),
                models=cleaned_models,
                note=" ".join((note or "").split())[:300] or None,
            )
            session.add(definition)
            session.flush()
            return _row(definition)
