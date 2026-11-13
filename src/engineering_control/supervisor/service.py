"""The supervisor's tools, served over the tool protocol its clients speak.

One endpoint, ``POST /mcp/<turn>``, speaking JSON-RPC as the Model Context Protocol's HTTP
transport defines it: every request is answered with one JSON document, and no stream is
opened. A request is accepted only with the token the launcher issued for that turn and
only while the turn runs; what a call may then do is decided by ``supervisor/tools.py``.

The service has the platform's database and, to read an issue's text, the GitHub App. It has
no identity towards the cluster. The only thing outside the control plane that can reach it
is the sandbox of a turn of the supervisor.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any, Final
from uuid import UUID

import structlog
import uvicorn
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engineering_control.config import Settings, get_settings
from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import ProjectRecords
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.models import SandboxRun, SandboxStep
from engineering_control.github_app import GitHubApp
from engineering_control.logging import configure_logging
from engineering_control.sandbox.runs import CONVERSATION, Runs
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import TOOLS, Definitions
from engineering_control.supervisor.tools import Toolbox
from engineering_control.supervisor.turn import token_digest

LOG = structlog.get_logger(__name__)
PROTOCOLS: Final = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY: Final = 256 * 1024
SERVER_INFO: Final = {"name": "engineering-control", "title": "Engineering Control", "version": "1"}


def toolbox_from(sessions: sessionmaker[Session], github: GitHubApp | None) -> Toolbox:
    definitions = Definitions(sessions)
    return Toolbox(
        sessions,
        projects=ProjectRecords(sessions),
        sections=SectionRecords(sessions),
        backlog=BacklogItems(sessions),
        accounts=Accounts(sessions),
        runs=Runs(sessions),
        conversation=Conversation(sessions, definitions),
        github=github,
    )


def _error(ident: Any, code: int, message: str, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _result(ident: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})


class ToolService:
    def __init__(
        self, sessions: sessionmaker[Session], toolbox: Toolbox, definitions: Definitions
    ) -> None:
        self.sessions = sessions
        self.toolbox = toolbox
        self.definitions = definitions

    def _turn(self, run_id: UUID, token: str) -> dict[str, Any] | None:
        """The definition the turn runs with, when the token is the one issued for it and
        the turn is still running. None otherwise, whatever the reason."""
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            if run is None or run.kind != CONVERSATION or run.state != "running":
                return None
            # The token is the step's: once its sandbox has ended it opens nothing.
            working = session.scalar(
                select(func.count())
                .select_from(SandboxStep)
                .where(SandboxStep.run_id == run.id, SandboxStep.state == "running")
            )
            if not working:
                return None
            expected = str((run.data or {}).get("tools_token") or "")
            version = int((run.data or {}).get("definition_version") or 0)
        if not expected or not hmac.compare_digest(expected, token_digest(token)):
            return None
        return self.definitions.get(version)

    async def handle(self, request: Request) -> Response:
        try:
            run_id = UUID(request.path_params["run_id"])
        except ValueError:
            return Response(status_code=404)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        definition = (
            await asyncio.to_thread(self._turn, run_id, token.strip())
            if scheme.lower() == "bearer" and token.strip()
            else None
        )
        if definition is None:
            LOG.warning("tools_refused", run=str(run_id))
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if request.method != "POST":
            # No stream is offered and there is no session to end.
            return Response(status_code=405, headers={"Allow": "POST"})
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _error(None, -32600, "The request is too large", status=413)
        try:
            body = await request.json()
        except ValueError:
            return _error(None, -32700, "The request is not JSON", status=400)
        if not isinstance(body, dict):
            return _error(None, -32600, "One request at a time", status=400)
        method, ident = body.get("method"), body.get("id")
        given = body.get("params")
        params: dict[str, Any] = given if isinstance(given, dict) else {}
        if ident is None:
            # A notification: nothing is answered.
            return Response(status_code=202)
        if method == "initialize":
            wanted = str(params.get("protocolVersion") or "")
            return _result(
                ident,
                {
                    "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            listed = [
                TOOLS[name].listed()
                for name, choice in definition["tools"].items()
                if choice.get("enabled") and name in TOOLS
            ]
            LOG.info("tools_listed", run=str(run_id), tools=len(listed))
            return _result(ident, {"tools": listed})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            outcome = await self.toolbox.call(
                run_id, definition, name, arguments if isinstance(arguments, dict) else {}
            )
            LOG.info("tool_called", run=str(run_id), tool=name[:40], error=outcome.error)
            return _result(
                ident,
                {"content": [{"type": "text", "text": outcome.text}], "isError": outcome.error},
            )
        return _error(ident, -32601, "There is no such method")


async def _health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def create_app(sessions: sessionmaker[Session], github: GitHubApp | None = None) -> Starlette:
    service = ToolService(sessions, toolbox_from(sessions, github), Definitions(sessions))
    return Starlette(
        routes=[
            Route("/mcp/{run_id}", service.handle, methods=["GET", "POST", "DELETE"]),
            Route("/healthz", _health, methods=["GET"]),
        ]
    )


def run(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)
    database_url = resolved.database_url.get_secret_value()
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
    )
    app = create_app(
        sessionmaker(engine, expire_on_commit=False), GitHubApp.from_settings(resolved)
    )
    uvicorn.run(
        app,
        host=resolved.supervisor_tools_host,
        port=resolved.supervisor_tools_port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    run()
