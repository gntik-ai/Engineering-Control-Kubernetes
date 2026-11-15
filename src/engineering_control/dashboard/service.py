from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from engineering_control.agent_models import choices as model_choices
from engineering_control.cluster import Cluster
from engineering_control.dashboard.accounts import (
    check_credential,
    provider_catalogue,
    secret_name,
)
from engineering_control.dashboard.project_list import project_list
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    clean_application_hosts,
    clean_application_url,
)
from engineering_control.dashboard.project_sections import Guide
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.engine.builtin import BUILTIN as BUILTIN_FLOWS
from engineering_control.engine.catalogue import BUDGETS
from engineering_control.engine.catalogue import catalogue as task_catalogue
from engineering_control.engine.conditions import catalogue as condition_catalogue
from engineering_control.engine.document import FORMAT as FLOW_FORMAT
from engineering_control.engine.document import FlowInvalid
from engineering_control.engine.document import parse as parse_flow
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.engine.records import described
from engineering_control.engine.simulate import recorded, simulate
from engineering_control.flow_library import flow_catalogue as library
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.models.providers import check_key, is_api
from engineering_control.models.sealing import seal
from engineering_control.models.service import Models
from engineering_control.sandbox.deliver import PROVIDERS as AGENT_PROVIDERS
from engineering_control.sandbox.deliver import toolchain_for
from engineering_control.sandbox.pods import identity_secret
from engineering_control.supervisor.definition import PROVIDERS as SUPERVISOR_PROVIDERS
from engineering_control.supervisor.definition import catalogue as tool_catalogue


class DashboardService:
    """Bounded read facade kept separate from HTTP and persistence details."""

    def __init__(self, repository: DashboardRepository) -> None:
        self.repository = repository

    async def projects(self) -> dict[str, object]:
        return await asyncio.to_thread(project_list, self.repository.sessions)

    async def project_exists(self, name: str) -> bool:
        return await asyncio.to_thread(self.repository.projects.exists, name)

    async def sync_registry_projects(self) -> list[str]:
        added = await asyncio.to_thread(self.repository.projects.sync_registry)
        # Every project has its core sections, whenever it was created.
        await asyncio.to_thread(self.repository.sections.sync_core)
        return added

    async def project(self, name: str) -> dict[str, object] | None:
        """One project with its counts and its sections; None when there is none."""
        listing = await asyncio.to_thread(project_list, self.repository.sessions)
        for item in cast(list[dict[str, object]], listing["items"]):
            if item["project"] == name:
                sections = await asyncio.to_thread(self.repository.sections.of_project, name)
                work = await asyncio.to_thread(self.repository.bring_over.status, name)
                application = await asyncio.to_thread(self.repository.projects.application, name)
                return {
                    "project": item,
                    "sections": sections,
                    "work": work,
                    "application": application,
                }
        return None

    async def project_commands(self, project: str) -> dict[str, object]:
        """How the project is installed and tested, as the next run would be told."""
        return await asyncio.to_thread(self.repository.projects.commands, project)

    async def set_project_commands(
        self, project: str, *, install: list[str], tests: list[str], platform: bool
    ) -> dict[str, object]:
        projects = self.repository.projects
        if platform:
            return await asyncio.to_thread(projects.forget_commands, project)
        return await asyncio.to_thread(
            lambda: projects.set_commands(project, install=install, tests=tests)
        )

    async def try_project_commands(self, project: str) -> dict[str, object]:
        """Run the project's installation and its tests once, without an agent."""
        return await asyncio.to_thread(
            lambda: self.repository.runs.start_commands_check(
                project, toolchain=toolchain_for(project).as_dict()
            )
        )

    async def set_application(
        self,
        project: str,
        *,
        url: str,
        username: str,
        password: str,
        forget_identity: bool,
        cluster: Cluster | None,
        hosts: list[str] | None = None,
    ) -> dict[str, object]:
        """Where the project's application runs, and the identity a step signs in to it
        with. The identity goes to the cluster as a Secret of the sandbox namespace and
        is not kept anywhere else; the record only says that there is one."""
        projects = self.repository.projects
        address = clean_application_url(url)
        others = clean_application_hosts(hosts) if hosts is not None else None
        project_id = await asyncio.to_thread(projects.identity_of, project)
        secret = identity_secret(project_id)
        identity: bool | None = None
        if username or password:
            if not (username and password):
                raise ProjectInvalid("A test identity is a user name and a password")
            if address is None:
                raise ProjectInvalid("Say where the application runs before its identity")
            if cluster is None:
                raise ProjectInvalid(
                    "This installation has no sandbox namespace, so an identity cannot be stored"
                )
            await cluster.put_secret(
                secret,
                {"username": username.encode(), "password": password.encode()},
                {
                    "app.kubernetes.io/part-of": "engineering-control",
                    "engineering-control/project": str(project_id),
                },
            )
            identity = True
        elif forget_identity or address is None:
            if cluster is not None:
                await cluster.delete_secret(secret)
            identity = False
        return await asyncio.to_thread(
            lambda: projects.set_application(project, url=address, hosts=others, identity=identity)
        )

    async def pause_project(self, project: str, reason: str) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.projects.pause(project, reason=reason, by="administrator")
        )

    async def resume_project(self, project: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.projects.resume, project)

    async def work_status(self, project: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.bring_over.status, project)

    async def preview_records(self, project: str, records: dict[str, Any]) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.bring_over.preview, project, records)

    async def bring_over(
        self, project: str, records: dict[str, Any], *, even_if_running: bool = False
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.bring_over.apply(
                project, records, even_if_running=even_if_running
            )
        )

    async def cut_over(self, project: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.bring_over.cut_over, project)

    async def add_section(self, project: str, *, name: str, guide: Guide) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.sections.add(project, name=name, guide=guide)
        )

    async def update_section(
        self, project: str, key: str, *, name: str | None, guide: Guide
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.sections.update(project, key, name=name, guide=guide)
        )

    async def routing_brief(self, project: str) -> str:
        return await asyncio.to_thread(self.repository.sections.brief, project)

    async def remove_section(self, project: str, key: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.sections.remove, project, key)

    async def assign_flow(self, project: str, key: str, flow: str | None) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.sections.assign, project, key, flow)

    async def items(
        self,
        *,
        project: str | None,
        section: str | None,
        state: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        def read() -> dict[str, Any]:
            listing = self.repository.backlog.listing(
                project=project,
                section=section,
                state=state,
                search=search,
                page=page,
                page_size=page_size,
            )
            # Each item with its latest run, so the backlog shows what is being worked on.
            ids = [item["id"] for item in listing["items"]]
            runs = self.repository.runs.for_items(ids)
            queued = self.repository.queues.for_items(ids)
            for item in listing["items"]:
                item["run"] = runs.get(item["id"])
                item["queue"] = queued.get(item["id"])
            return listing

        return await asyncio.to_thread(read)

    async def deliver_item(self, item_id: UUID) -> dict[str, object]:
        """Start the run the item's section says: a delivery, or the audit it asks for."""

        def start() -> dict[str, Any]:
            project = self.repository.backlog.project_of(item_id)
            toolchain = toolchain_for(project).as_dict() if project else {}
            return self.repository.runs.start_item(item_id, toolchain=toolchain)

        return await asyncio.to_thread(start)

    async def run_section(self, project: str, section: str, scope: str) -> dict[str, object]:
        """Start what the section runs by itself: an audit, or a search for enhancements."""
        return await asyncio.to_thread(
            lambda: self.repository.runs.start_for_section(
                project, section, scope=scope, toolchain=toolchain_for(project).as_dict()
            )
        )

    async def item(self, item_id: UUID) -> dict[str, object]:
        def read() -> dict[str, Any]:
            found = self.repository.backlog.get(item_id)
            found["run"] = self.repository.runs.for_items([found["id"]]).get(found["id"])
            found["queue"] = self.repository.queues.for_items([found["id"]]).get(found["id"])
            return found

        return await asyncio.to_thread(read)

    async def edit_item(
        self, item_id: UUID, *, title: str | None, description: str | None
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.backlog.edit(item_id, title=title, description=description)
        )

    async def decide_run(self, run_id: UUID, decision: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.runs.decide, run_id, decision)

    async def add_item(
        self, project: str, *, title: str, section: str | None, description: str | None = None
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.backlog.add(
                project, title=title, section=section, description=description
            )
        )

    async def route_item(self, item_id: UUID, section: str | None) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.backlog.route, item_id, section)

    async def set_item_state(self, item_id: UUID, state: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.backlog.set_state, item_id, state)

    async def inbox(self) -> dict[str, object]:
        # An account nobody can work with stops everything, so it comes first.
        entries = await asyncio.to_thread(self.repository.toolbox.inbox_entries)
        return {"items": entries, "total": len(entries)}

    # --- queues and budgets --------------------------------------------------------------

    async def queues(self, *, project: str | None = None) -> dict[str, object]:
        return await asyncio.to_thread(lambda: self.repository.queues.listing(project=project))

    async def queue(self, queue_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.queues.get, queue_id)

    async def create_queue(
        self, project: str, *, name: str, items: list[UUID], at_once: int
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.queues.create(
                project, name=name, item_ids=items, at_once=at_once
            )
        )

    async def change_queue(self, queue_id: UUID, change: str, **fields: Any) -> dict[str, object]:
        """One change to a queue, by name: add, order, remove, update, start, pause, discard."""
        queues = self.repository.queues

        def apply() -> dict[str, Any]:
            if change == "add":
                return queues.add_items(queue_id, fields["items"])
            if change == "order":
                return queues.reorder(queue_id, fields["items"])
            if change == "remove":
                return queues.remove_item(queue_id, fields["item"])
            if change == "update":
                return queues.update(
                    queue_id, name=fields.get("name"), at_once=fields.get("at_once")
                )
            if change == "start":
                return queues.start(queue_id)
            if change == "pause":
                return queues.pause(queue_id)
            if change == "discard":
                return queues.discard(queue_id)
            raise ProjectInvalid("That is not a change a queue can take")

        return await asyncio.to_thread(apply)

    async def budgets(self) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.budgets.listing)

    async def set_budget(
        self, scope: str, scope_id: UUID | None, *, at_once: int | None, per_day: int | None
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.budgets.save(scope, scope_id, at_once=at_once, per_day=per_day)
        )

    # --- the supervisor ------------------------------------------------------------------

    async def supervisor(self, *, after: datetime | None = None) -> dict[str, object]:
        """The conversation, whether the supervisor is answering, and what it proposed
        that waits for the administrator."""

        def read() -> dict[str, Any]:
            listing = self.repository.conversation.listing(after=after)
            return {**listing, "pending": self.repository.toolbox.pending()}

        return await asyncio.to_thread(read)

    async def say_to_supervisor(self, text: str) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: {
                **self.repository.conversation.post(text, channel="console"),
                "message": "console",
            }
        )

    async def stop_supervisor(self) -> dict[str, object]:
        def stop() -> dict[str, Any]:
            turn = self.repository.conversation.listing(limit=1)["turn"]
            if turn is None:
                raise ProjectConflict("The supervisor is not answering right now")
            return self.repository.runs.request_cancel(UUID(turn["run_id"]))

        return await asyncio.to_thread(stop)

    async def supervisor_settings(self) -> dict[str, object]:
        def read() -> dict[str, Any]:
            accounts = [
                {key: account[key] for key in ("id", "name", "provider", "state")}
                for account in self.repository.accounts.listing()["items"]
            ]
            return {
                "definition": self.repository.definitions.current(),
                "versions": self.repository.definitions.versions(),
                "catalogue": tool_catalogue(),
                "providers": list(SUPERVISOR_PROVIDERS),
                "accounts": accounts,
                "choices": model_choices(),
            }

        return await asyncio.to_thread(read)

    async def supervisor_version(self, version: int) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.definitions.get, version)

    async def save_supervisor(self, **fields: Any) -> dict[str, object]:
        def save() -> dict[str, Any]:
            saved = self.repository.definitions.save(**fields)
            return {**saved, "definition": f"version {saved['version']}"}

        return await asyncio.to_thread(save)

    async def choose_supervisor_model(
        self, provider: str, *, model: str | None, effort: str | None
    ) -> dict[str, object]:
        def choose() -> dict[str, Any]:
            saved = self.repository.definitions.choose(provider, model=model, effort=effort)
            return {**saved, "definition": f"version {saved['version']}"}

        return await asyncio.to_thread(choose)

    async def decide_action(self, action_id: UUID, accept: bool) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.toolbox.decide(action_id, accept=accept, via="console")
        )

    async def proposed_flow(self, action_id: UUID) -> dict[str, object]:
        """A flow the supervisor proposed, to look at before it is decided."""
        return await asyncio.to_thread(self.repository.toolbox.proposed_flow, action_id)

    async def incidents(self, *, everything: bool = False) -> dict[str, object]:
        """What went wrong with the work and what the supervisor made of it."""
        return await asyncio.to_thread(
            lambda: self.repository.operations.listing(everything=everything)
        )

    async def dismiss_incident(self, incident_id: UUID) -> dict[str, object]:
        def dismiss() -> dict[str, Any]:
            closed = self.repository.operations.dismiss(incident_id)
            return {**closed, "incident": closed["summary"]}

        return await asyncio.to_thread(dismiss)

    async def supervisor_record(self, *, everything: bool = False) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.toolbox.record(decisions_only=not everything)
        )

    async def accounts(self) -> dict[str, object]:
        def read() -> dict[str, Any]:
            listing = self.repository.accounts.listing()
            tried = self.repository.runs.accounts_being_tried()
            for item in listing["items"]:
                item["being_tried"] = item["id"] in tried
            return listing

        return {**await asyncio.to_thread(read), "providers": provider_catalogue()}

    async def try_account(self, account_id: UUID) -> dict[str, object]:
        account = await asyncio.to_thread(self.repository.accounts.get, account_id)
        if is_api(str(account["provider"])):
            raise ProjectInvalid(
                "An account with an API key is tried by asking its provider which models "
                "the key reaches, not with a run"
            )
        return await asyncio.to_thread(self.repository.runs.start_account_check, account_id)

    # --- triggers --------------------------------------------------------------------------

    async def triggers(self, project: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.triggers.of_project, project)

    async def create_trigger(
        self, project: str, section: str, kind: str, spec: dict[str, Any]
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.triggers.create(project, section, kind=kind, spec=spec)
        )

    async def update_trigger(
        self, trigger_id: UUID, enabled: bool | None, spec: dict[str, Any] | None
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.triggers.update(trigger_id, enabled=enabled, spec=spec)
        )

    async def remove_trigger(self, trigger_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.triggers.remove, trigger_id)

    async def fire_trigger(self, trigger_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.triggers.fire, trigger_id)

    # --- flows -----------------------------------------------------------------------------

    async def flows(self) -> dict[str, object]:
        """The library of flows. One that runs on this platform says so under ``engine``:
        its version in force, its draft, what it consists of."""
        running = {
            row["key"]: row for row in await asyncio.to_thread(self.repository.flows.summary)
        }
        listed = [{**flow, "engine": running.pop(flow["key"], None)} for flow in library()]
        # Flows made here that the library does not know.
        listed += [
            {"key": key, "name": row["name"], "summary": row["summary"], "engine": row}
            for key, row in running.items()
        ]
        return {"items": listed}

    async def flow_catalogue(self) -> dict[str, object]:
        """What a flow can be built from: the tasks, the prompts and the budgets."""
        return {
            "format": FLOW_FORMAT,
            "tasks": task_catalogue(),
            "prompts": [contract.describe({}) for contract in CONTRACTS.values()],
            "budgets": [param.as_dict() for param in BUDGETS.values()],
            "providers": list(AGENT_PROVIDERS),
            # The models an agent's task can be set to, and the levels each has.
            "models": model_choices(),
            # What an If can ask about, and the ways each is compared.
            "conditions": condition_catalogue(),
            "saved": await asyncio.to_thread(self.repository.tasks.saved),
            # The platform's kinds of task that are not offered in the designer's list.
            "hidden": await asyncio.to_thread(self.repository.tasks.hidden),
        }

    # --- the tasks themselves ------------------------------------------------------------

    async def tasks(self) -> dict[str, object]:
        """Every task a flow can be built from, with the flows that use it."""
        return await asyncio.to_thread(self.repository.tasks.listing)

    async def create_task(
        self,
        *,
        name: str,
        task: str | None,
        description: str | None,
        settings: dict[str, Any],
        wording: dict[str, str],
    ) -> dict[str, object]:
        if not task:
            raise ProjectInvalid("Say which kind of task it is")
        return await asyncio.to_thread(
            lambda: self.repository.tasks.create(
                name=name, task=task, description=description, params=settings, wording=wording
            )
        )

    async def update_task(
        self,
        saved_id: UUID,
        *,
        name: str,
        description: str | None,
        settings: dict[str, Any],
        wording: dict[str, str],
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.tasks.update(
                saved_id, name=name, description=description, params=settings, wording=wording
            )
        )

    async def remove_task(self, saved_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.tasks.remove, saved_id)

    async def apply_task(self, saved_id: UUID) -> dict[str, object]:
        """Write a task as it is now into a draft of every flow that uses it."""
        return await asyncio.to_thread(self.repository.tasks.apply, saved_id)

    async def hide_task(self, task: str, *, hidden: bool) -> dict[str, object]:
        return await asyncio.to_thread(lambda: self.repository.tasks.hide(task, hidden=hidden))

    async def check_flow(self, document: dict[str, Any]) -> dict[str, object]:
        """What the rules say about a flow as it is in the editor. Nothing is stored."""
        try:
            flow = parse_flow(document)
        except FlowInvalid as error:
            raise ProjectInvalid(str(error)) from error
        return described(flow)

    async def save_task(
        self, *, name: str, task: str, settings: dict[str, Any], wording: dict[str, str]
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.flows.save_task(
                name=name, task=task, params=settings, wording=wording
            )
        )

    async def remove_saved_task(self, saved_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.flows.remove_task, saved_id)

    async def flow(self, key: str) -> dict[str, object]:
        def read() -> dict[str, Any]:
            flows = self.repository.flows
            versions = flows.versions(key)
            draft = flows.draft(key)
            current = flows.version_in_force(key)
            return {
                "key": key,
                # None for a flow that was made here and never put in force.
                "in_force": flows.shown(key, current) if current is not None else None,
                "draft": flows.shown(key, draft) if draft is not None else None,
                "versions": versions,
                "builtin": key in BUILTIN_FLOWS,
                "used_by": self.repository.sections.using(key),
            }

        return await asyncio.to_thread(read)

    async def flow_version(self, key: str, version: int) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.flows.shown, key, version)

    async def save_flow_draft(
        self, key: str, document: dict[str, Any], note: str | None
    ) -> dict[str, object]:
        def save() -> dict[str, Any]:
            saved = self.repository.flows.save_draft(key, document, note=note)
            return {**self.repository.flows.shown(key, saved["version"]), "flow": key}

        return await asyncio.to_thread(save)

    async def create_flow(
        self,
        *,
        key: str,
        name: str,
        summary: str,
        copy_of: str | None,
        version: int | None,
        document: dict[str, Any] | None,
    ) -> dict[str, object]:
        """A new flow of the library: a document that is brought in, a copy of a flow
        that exists, or a flow with nothing in it yet. It starts as a draft."""

        def create() -> dict[str, Any]:
            flows = self.repository.flows
            if document is not None:
                raw = dict(document)
                if flows.exists(str(raw.get("key") or "")):
                    # A document of a flow that is here already becomes its draft.
                    saved = flows.save_draft(str(raw["key"]), raw, note="Brought in")
                    return {**flows.shown(str(raw["key"]), saved["version"]), "flow": raw["key"]}
            elif copy_of is not None:
                wanted = flows.version_in_force(copy_of) if version is None else version
                found = flows.document(
                    copy_of, wanted if wanted is not None else flows.draft(copy_of) or 0
                )
                if found is None:
                    raise ProjectNotFound(copy_of)
                raw = {**found.as_dict(), "key": key, "name": name, "summary": summary}
            else:
                raw = {
                    "format": FLOW_FORMAT,
                    "key": key,
                    "name": name,
                    "summary": summary,
                    "start": "prepare",
                    "tasks": [{"id": "prepare", "task": "workspace.clone"}],
                }
            made = flows.create(raw, note="Brought in" if document is not None else None)
            flow_key = str(raw.get("key"))
            return {**flows.shown(flow_key, made["version"]), "flow": flow_key}

        return await asyncio.to_thread(create)

    async def discard_flow_draft(self, key: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.flows.discard_draft, key)

    async def publish_flow(self, key: str, note: str | None) -> dict[str, object]:
        return await asyncio.to_thread(lambda: self.repository.flows.publish(key, note=note))

    async def simulate_flow(
        self,
        key: str,
        *,
        document: dict[str, Any] | None,
        version: int | None,
        run_id: UUID | None,
        outcomes: dict[str, list[str]],
        scope: bool,
        entries: int,
        max_rounds: int | None,
    ) -> dict[str, object]:
        """Go through a flow on paper. Nothing is stored and nothing runs."""

        def go() -> dict[str, Any]:
            flows = self.repository.flows
            if document is not None:
                try:
                    flow = parse_flow(document)
                except FlowInvalid as error:
                    raise ProjectInvalid(str(error)) from error
            else:
                current = flows.version_in_force(key) if version is None else version
                wanted = current if current is not None else flows.draft(key) or 0
                found = flows.document(key, wanted)
                if found is None:
                    raise ProjectNotFound(f"{key} version {wanted}")
                flow = found
            told = dict(outcomes)
            of_run = None
            given, listed = scope, entries
            knew: dict[str, Any] = {}
            if run_id is not None:
                run = self.repository.runs.get(run_id)
                told = {**recorded(run["steps"], run["data"]), **told}
                of_run = {
                    "id": run["id"],
                    "title": run["title"],
                    "state": run["state"],
                    "outcome": run["outcome"],
                    "flow_version": (run["data"].get("flow") or {}).get("version", 0),
                    "known": sum(len(ways) for ways in told.values()),
                }
                scoped = run["data"].get("scope")
                if isinstance(scoped, dict):
                    given = bool(str(scoped.get("title") or scoped.get("text") or "").strip())
                areas = run["data"].get("areas")
                if isinstance(areas, list):
                    listed = len(areas)
                # What the Ifs of the flow ask about, as that run knew it in the end.
                knew = dict(run["data"])
            result = simulate(
                flow,
                outcomes=told,
                scope=given,
                entries=listed,
                max_rounds=max_rounds,
                known=knew,
            )
            return {**result, "flow": key, "sha256": flow.sha256(), "of_run": of_run}

        return await asyncio.to_thread(go)

    async def usage(self, days: int) -> dict[str, object]:
        return await asyncio.to_thread(lambda: self.repository.runs.usage_totals(days=days))

    async def evaluations(self, key: str) -> dict[str, object]:
        def read() -> dict[str, Any]:
            return {
                "items": self.repository.evaluations.listing(key),
                "candidates": self.repository.evaluations.candidates(key),
            }

        return await asyncio.to_thread(read)

    async def start_evaluation(
        self, key: str, *, version: int, run_ids: list[UUID], note: str | None
    ) -> dict[str, object]:
        def start() -> dict[str, Any]:
            started = self.repository.evaluations.start(
                key, version=version, run_ids=run_ids, note=note
            )
            return {**started, "evaluation": f"{key} version {version}"}

        return await asyncio.to_thread(start)

    async def evaluation(self, evaluation_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.evaluations.get, evaluation_id)

    async def stop_evaluation(self, evaluation_id: UUID) -> dict[str, object]:
        def stop() -> dict[str, Any]:
            stopped = self.repository.evaluations.stop(evaluation_id)
            return {
                **stopped,
                "evaluation": f"{stopped['flow']} version {stopped['candidate_version']}",
            }

        return await asyncio.to_thread(stop)

    async def runs(self, *, project: str | None = None) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.runs.listing, project=project)

    async def run(self, run_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.runs.get, run_id)

    async def run_flow(self, run_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.runs.flow_of, run_id)

    async def cancel_run(self, run_id: UUID) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.runs.request_cancel, run_id)

    async def create_account(
        self, *, name: str, provider: str, endpoint: str | None = None
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.accounts.create(name=name, provider=provider, endpoint=endpoint)
        )

    async def update_account(self, account_id: UUID, **changes: Any) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.accounts.update(account_id, **changes)
        )

    async def sign_in_account(
        self,
        account_id: UUID,
        pasted: str,
        cluster: Cluster | None,
        models: Models | None = None,
    ) -> dict[str, object]:
        """Check what was pasted, hand it to the cluster, then record that it is there.
        An API key is sealed into the account's record instead, and tried at once."""
        account = await asyncio.to_thread(self.repository.accounts.get, account_id)
        if is_api(str(account["provider"])):
            try:
                key = check_key(str(account["provider"]), pasted)
            except ValueError as error:
                raise ProjectInvalid(str(error)) from error
            if models is None or models.sealing_key is None:
                raise ProjectInvalid(
                    "This installation has no key to seal API keys with, so one cannot be stored"
                )
            sealed = seal(models.sealing_key, account_id, key)
            await asyncio.to_thread(self.repository.accounts.key_stored, account_id, sealed)
            return await self.check_account(account_id, models)
        credential = check_credential(str(account["provider"]), pasted)
        if cluster is None:
            raise ProjectInvalid(
                "This installation has no sandbox namespace, so a sign-in cannot be stored"
            )
        await cluster.put_secret(
            secret_name(account_id),
            credential.data,
            {
                "app.kubernetes.io/part-of": "engineering-control",
                "engineering-control/account": str(account_id),
                "engineering-control/provider": str(account["provider"]),
            },
        )
        return await asyncio.to_thread(
            self.repository.accounts.signed_in, account_id, credential.identity
        )

    async def check_account(self, account_id: UUID, models: Models | None) -> dict[str, object]:
        """Ask the provider which models the account's key reaches: whether the key is
        accepted, at no cost."""
        account = await asyncio.to_thread(self.repository.accounts.get, account_id)
        if not is_api(str(account["provider"])):
            raise ProjectInvalid("That account is tried with a run, not asked for its models")
        if models is None:
            raise ProjectInvalid("This installation cannot ask a provider")
        await models.check(account_id)
        return await asyncio.to_thread(self.repository.accounts.get, account_id)

    async def remove_account(self, account_id: UUID, cluster: Cluster | None) -> dict[str, object]:
        """The sign-in leaves the cluster first; the record is removed only when it is gone."""
        account = await asyncio.to_thread(self.repository.accounts.get, account_id)
        if await asyncio.to_thread(self.repository.runs.account_in_use, account_id):
            raise ProjectConflict(
                f"{account['name']} is in use by a run. Stop the run or wait for it to end."
            )
        if cluster is not None and not is_api(str(account["provider"])):
            await cluster.delete_secret(secret_name(account_id))
        return await asyncio.to_thread(self.repository.accounts.archive, account_id)

    async def bulk_items(self, project: str, **options: Any) -> dict[str, object]:
        return await asyncio.to_thread(lambda: self.repository.backlog.bulk(project, **options))

    async def backlog_status(self, project: str) -> dict[str, object] | None:
        return await asyncio.to_thread(self.repository.backlog.sync_status, project)

    async def sync_backlog(self, project: str, github: GitHubApp) -> dict[str, object]:
        """Read the open issues of the project's repository and bring its items in step."""
        repository = await asyncio.to_thread(self.repository.backlog.repository, project)
        try:
            issues, complete = await github.open_issues(repository)
        except GitHubAppError as error:
            await asyncio.to_thread(self.repository.backlog.record_failure, project, str(error))
            raise
        return await asyncio.to_thread(
            lambda: self.repository.backlog.apply_import(project, issues, complete=complete)
        )

    async def active_project_names(self) -> list[str]:
        return await asyncio.to_thread(
            lambda: [record.name for record in self.repository.projects.active()]
        )

    async def create_project(
        self,
        *,
        name: str,
        display_name: str | None,
        repository: str,
        default_branch: str = "main",
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.projects.create(
                name=name,
                display_name=display_name,
                repository=repository,
                default_branch=default_branch,
            )
        )

    async def connected_repositories(self) -> dict[str, str]:
        return await asyncio.to_thread(self.repository.projects.connected_repositories)

    async def rename_project(self, name: str, display_name: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.projects.rename, name, display_name)

    async def archive_project(self, name: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.projects.archive, name)
