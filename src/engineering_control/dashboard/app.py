from __future__ import annotations

import grp
import hmac
import ipaddress
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from jinja2 import StrictUndefined
from starlette.templating import Jinja2Templates

from engineering_control.config import Settings, get_settings
from engineering_control.dashboard.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    LoginFailureLimiter,
    SignedSessionManager,
    read_session_key,
)
from engineering_control.dashboard.dependencies import AuthenticatedSession
from engineering_control.dashboard.events import sse_events, transcript_sse
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.schemas import (
    ActionCandidateInput,
    ActionExecuteInput,
    ActionReauthenticationInput,
    ActivityEventInput,
    PasswordChangeInput,
)
from engineering_control.dashboard.service import DashboardService
from engineering_control.dashboard.status_mapping import NORMALIZED_STATUSES
from engineering_control.logging import configure_logging
from engineering_control.operations.models import parse_command
from engineering_control.operations.registry import COMMAND_REGISTRY
from engineering_control.operations.repository import OperationsRepository

PACKAGE_ROOT = Path(__file__).parent
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
STATIC_ROOT = PACKAGE_ROOT / "static"
PROJECTS = frozenset({"falcone", "musematic", "llmwiki"})


def create_app(
    *,
    settings: Settings | None = None,
    repository: DashboardRepository | None = None,
    session_key: bytes | None = None,
    limiter: LoginFailureLimiter | None = None,
    operations_repository: OperationsRepository | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    key = session_key
    if key is None:
        group_id = grp.getgrnam("engineering-control").gr_gid
        key = read_session_key(resolved.dashboard_session_key_file, expected_group_id=group_id)
    sessions = SignedSessionManager(
        key,
        lifetime_seconds=resolved.dashboard_session_hours * 3600,
    )
    login_limiter = limiter or LoginFailureLimiter()
    templates = Jinja2Templates(directory=TEMPLATE_ROOT)
    templates.env.undefined = StrictUndefined

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_repository = repository or DashboardRepository(
            resolved.database_url.get_secret_value()
        )
        app.state.dashboard_repository = resolved_repository
        app.state.dashboard_service = DashboardService(resolved_repository)
        resolved_operations = operations_repository or OperationsRepository(
            resolved.database_url.get_secret_value(),
            export_retention_days=resolved.dashboard_operation_export_retention_days,
            export_max_bytes=resolved.dashboard_operation_export_max_bytes,
        )
        app.state.operations_repository = resolved_operations
        yield
        if repository is None:
            resolved_repository.engine.dispose()
        if operations_repository is None:
            resolved_operations.engine.dispose()

    app = FastAPI(
        title="Hermes Engineering Operations Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = cast(Response, await call_next(request))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
            "object-src 'none'; img-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'"
        )
        return response

    def repo(request: Request) -> DashboardRepository:
        return cast(DashboardRepository, request.app.state.dashboard_repository)

    def service(request: Request) -> DashboardService:
        return cast(DashboardService, request.app.state.dashboard_service)

    def operations(request: Request) -> OperationsRepository:
        return cast(OperationsRepository, request.app.state.operations_repository)

    async def current_session(request: Request) -> AuthenticatedSession | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        data = sessions.verify(token)
        if data is None:
            return None
        version = await repo(request).authentication_version()
        if version is None or version != data.password_version:
            return None
        return AuthenticatedSession(token=token, data=data)

    async def require_session(
        request: Request, *, api: bool = False
    ) -> AuthenticatedSession | Response:
        authenticated = await current_session(request)
        if authenticated is not None:
            return authenticated
        if api:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        destination = request.url.path
        target = "/login"
        if destination not in {"/", "/login"}:
            target = f"/login?next={destination}"
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    def page_context(
        request: Request,
        authenticated: AuthenticatedSession,
        *,
        title: str,
        active: str,
        error: str | None = None,
        notice: str | None = None,
    ) -> dict[str, Any]:
        return {
            "request": request,
            "title": title,
            "active": active,
            "csrf_token": authenticated.data.csrf_token,
            "error": error,
            "notice": notice,
            "projects": sorted(PROJECTS),
            "statuses": NORMALIZED_STATUSES,
        }

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> Response:
        repository_ready = await repo(request).ping()
        initialized = await repo(request).authentication_version()
        if not repository_ready or initialized is None:
            return JSONResponse({"status": "not-ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/robots.txt")
    async def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if request.query_params.get("asset") == "css":
            return FileResponse(STATIC_ROOT / "dashboard.css", media_type="text/css")
        if request.query_params.get("asset") == "icon":
            return Response(status_code=204)
        if await current_session(request) is not None:
            return RedirectResponse("/overview", status_code=303)
        csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        if not csrf or not sessions.verify_login_csrf(csrf):
            csrf = sessions.create_login_csrf()
        next_path = _safe_next(request.query_params.get("next"))
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "title": "Sign in",
                "csrf_token": csrf,
                "error": None,
                "next_path": next_path,
            },
        )
        _set_cookie(response, LOGIN_CSRF_COOKIE, csrf, max_age=900)
        return response

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        form = await _form(request)
        csrf = form.get("csrf_token", "")
        csrf_cookie = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        next_path = _safe_next(form.get("next_path"))
        address = _client_address(request)
        csrf_valid = (
            bool(csrf)
            and bool(csrf_cookie)
            and hmac.compare_digest(csrf, csrf_cookie)
            and sessions.verify_login_csrf(csrf_cookie)
        )
        blocked = login_limiter.blocked(address)
        password = form.get("password", "")
        version = None
        if csrf_valid and not blocked and len(password) <= 128:
            version = await repo(request).verify_password(password)
        if version is None:
            if csrf_valid and not blocked:
                login_limiter.failure(address)
            fresh_csrf = sessions.create_login_csrf()
            response = templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "title": "Sign in",
                    "csrf_token": fresh_csrf,
                    "error": "Unable to sign in.",
                    "next_path": next_path,
                },
                status_code=429 if blocked else 401,
            )
            _set_cookie(response, LOGIN_CSRF_COOKIE, fresh_csrf, max_age=900)
            return response
        login_limiter.success(address)
        token = sessions.create(version)
        redirect = RedirectResponse(next_path or "/overview", status_code=303)
        _set_cookie(
            redirect, SESSION_COOKIE, token, max_age=resolved.dashboard_session_hours * 3600
        )
        redirect.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
        await _record_safely(
            repo(request),
            ActivityEventInput(
                level="INFO",
                event_type="dashboard.login_succeeded",
                source="dashboard",
                message="Dashboard authentication succeeded",
            ),
        )
        return redirect

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        form = await _form(request)
        if not _valid_csrf(authenticated, form.get("csrf_token")):
            return PlainTextResponse("invalid request", status_code=403)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/")
    async def root(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return RedirectResponse("/overview", status_code=303)

    @app.get("/overview", response_class=HTMLResponse)
    async def overview_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="overview.html",
            context=page_context(request, authenticated, title="Overview", active="overview"),
        )

    @app.get("/backlog", response_class=HTMLResponse)
    async def backlog_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="backlog.html",
            context=page_context(request, authenticated, title="Backlog", active="backlog"),
        )

    @app.get("/activity")
    async def activity_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return RedirectResponse("/logs", status_code=303)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context=page_context(request, authenticated, title="Logs", active="logs"),
        )

    @app.get("/workflows", response_class=HTMLResponse)
    async def workflows_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="workflows.html",
            context=page_context(request, authenticated, title="Workflows", active="workflows"),
        )

    @app.get("/workflows/{workflow_id}", response_class=HTMLResponse)
    async def workflow_page(workflow_id: str, request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        if not 1 <= len(workflow_id) <= 240:
            raise HTTPException(status_code=404, detail="workflow not found")
        return templates.TemplateResponse(
            request=request,
            name="workflow.html",
            context={
                **page_context(request, authenticated, title="Workflow detail", active="workflows"),
                "workflow_id": workflow_id,
            },
        )

    @app.get("/executions/{execution_id}", response_class=HTMLResponse)
    async def execution_page(execution_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="execution.html",
            context={
                **page_context(request, authenticated, title="Execution", active="logs"),
                "execution_id": str(execution_id),
            },
        )

    @app.get("/audits", response_class=HTMLResponse)
    async def audits_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="audits.html",
            context=page_context(request, authenticated, title="Audits", active="audits"),
        )

    @app.get("/commands", response_class=HTMLResponse)
    async def commands_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="commands.html",
            context=page_context(request, authenticated, title="Commands", active="commands"),
        )

    @app.get("/findings", response_class=HTMLResponse)
    async def findings_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="findings.html",
            context=page_context(request, authenticated, title="Findings", active="findings"),
        )

    @app.get("/enhancements", response_class=HTMLResponse)
    async def enhancements_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="enhancements.html",
            context=page_context(
                request, authenticated, title="Enhancements", active="enhancements"
            ),
        )

    @app.get("/connections", response_class=HTMLResponse)
    async def connections_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="connections.html",
            context=page_context(request, authenticated, title="Connections", active="connections"),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=page_context(request, authenticated, title="Settings", active="settings"),
        )

    @app.post("/settings/password", response_class=HTMLResponse)
    async def change_password(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        form = await _form(request)
        if not _valid_csrf(authenticated, form.get("csrf_token")):
            return PlainTextResponse("invalid request", status_code=403)
        try:
            values = PasswordChangeInput(
                current_password=form.get("current_password", ""),
                new_password=form.get("new_password", ""),
                confirmation=form.get("confirmation", ""),
            )
            if values.new_password != values.confirmation:
                raise ValueError("new password confirmation does not match")
            version = await repo(request).change_password(
                values.current_password, values.new_password
            )
        except ValueError as error:
            return templates.TemplateResponse(
                request=request,
                name="settings.html",
                context=page_context(
                    request,
                    authenticated,
                    title="Settings",
                    active="settings",
                    error=str(error),
                ),
                status_code=400,
            )
        except Exception:  # noqa: BLE001 - form values must never enter error logging
            version = None
        if version is None:
            return templates.TemplateResponse(
                request=request,
                name="settings.html",
                context=page_context(
                    request,
                    authenticated,
                    title="Settings",
                    active="settings",
                    error="Unable to change password.",
                ),
                status_code=400,
            )
        token = sessions.create(version)
        updated = AuthenticatedSession(token=token, data=cast(Any, sessions.verify(token)))
        response = templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=page_context(
                request,
                updated,
                title="Settings",
                active="settings",
                notice="Password changed. Other sessions have been signed out.",
            ),
        )
        _set_cookie(
            response, SESSION_COOKIE, token, max_age=resolved.dashboard_session_hours * 3600
        )
        await _record_safely(
            repo(request),
            ActivityEventInput(
                level="INFO",
                event_type="dashboard.password_changed",
                source="dashboard",
                message="Dashboard password version changed",
            ),
        )
        return response

    @app.get("/api/dashboard/backlog")
    async def backlog_api(
        request: Request,
        project: str | None = None,
        status: str | None = None,
        search: str | None = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1, le=10_000),
        page_size: int = Query(default=50, ge=10, le=100),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_filters(project, status)
        result = await service(request).backlog(
            project=project,
            status=status,
            search=search,
            page=page,
            page_size=page_size,
        )
        return JSONResponse(result)

    @app.get("/api/dashboard/overview")
    async def overview_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).overview())

    @app.get("/api/dashboard/workflows")
    async def workflows_api(
        request: Request,
        project: str | None = None,
        workflow_type: str | None = Query(default=None, max_length=80),
        status: str | None = None,
        provider: str | None = None,
        role: str | None = Query(default=None, max_length=80),
        worker: str | None = Query(default=None, max_length=32),
        search: str | None = Query(default=None, max_length=200),
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        cursor: str | None = Query(default=None, max_length=1000),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_observability_filters(project, status, provider)
        try:
            value = await service(request).workflows(
                project=project,
                workflow_type=workflow_type,
                status=status,
                provider=provider,
                role=role,
                worker=worker,
                search=search,
                date_from=date_from,
                date_to=date_to,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JSONResponse(value)

    @app.get("/api/dashboard/workflows/{workflow_id}")
    async def workflow_api(workflow_id: str, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        if not 1 <= len(workflow_id) <= 240:
            raise HTTPException(status_code=404, detail="workflow not found")
        value = await service(request).workflow(workflow_id)
        if value is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return JSONResponse(value)

    @app.get("/api/dashboard/executions")
    async def executions_api(
        request: Request,
        project: str | None = None,
        workflow_id: str | None = Query(default=None, max_length=240),
        issue_number: int | None = Query(default=None, ge=1),
        campaign_id: UUID | None = None,
        provider: str | None = None,
        role: str | None = Query(default=None, max_length=80),
        status: str | None = None,
        worker: str | None = Query(default=None, max_length=32),
        host: str | None = Query(default=None, max_length=253),
        phase: str | None = Query(default=None, max_length=120),
        search: str | None = Query(default=None, max_length=200),
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        cursor: str | None = Query(default=None, max_length=1000),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_observability_filters(project, status, provider)
        try:
            value = await service(request).executions(
                project=project,
                workflow_id=workflow_id,
                issue_number=issue_number,
                campaign_id=campaign_id,
                provider=provider,
                role=role,
                status=status,
                worker=worker,
                host=host,
                phase=phase,
                search=search,
                date_from=date_from,
                date_to=date_to,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JSONResponse(value)

    @app.get("/api/dashboard/executions/{execution_id}")
    async def execution_api(execution_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        value = await service(request).execution(execution_id)
        if value is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return JSONResponse(value)

    @app.get("/api/dashboard/executions/{execution_id}/logs")
    async def execution_logs_api(
        execution_id: UUID,
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
        before_sequence: int | None = Query(default=None, ge=1),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        value = await service(request).execution_logs(
            execution_id,
            limit=limit,
            before_sequence=before_sequence,
        )
        if value is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return JSONResponse(value)

    @app.get("/api/dashboard/executions/{execution_id}/stream")
    async def execution_stream_api(execution_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        if await service(request).execution(execution_id) is None:
            raise HTTPException(status_code=404, detail="execution not found")
        raw_id = request.headers.get("last-event-id", "0")
        try:
            after_sequence = max(0, int(raw_id))
        except ValueError:
            after_sequence = 0
        response = StreamingResponse(
            transcript_sse(
                repo(request).observability,
                execution_id,
                after_sequence=after_sequence,
                disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.get("/api/dashboard/services")
    async def services_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).services())

    @app.get("/api/dashboard/root-blockers")
    async def root_blockers_api(
        request: Request,
        project: str | None = None,
        campaign_id: UUID | None = None,
        cursor: str | None = Query(default=None, max_length=1000),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_filters(project, None)
        try:
            value = await service(request).root_blockers(
                project=project,
                campaign_id=campaign_id,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JSONResponse(value)

    @app.get("/api/dashboard/audits")
    async def audits_api(
        request: Request, project: str | None = None, status: str | None = None
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_filters(project, status)
        return JSONResponse(await service(request).audits(project=project, status=status))

    @app.get("/api/dashboard/commands")
    async def commands_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse({"items": COMMAND_REGISTRY})

    @app.get("/api/dashboard/r3-issues/candidate")
    async def dashboard_r3_issue_candidate_api(
        request: Request,
        project: str = Query(pattern=r"^(falcone|musematic|llmwiki)$"),
        surface: str = Query(pattern=r"^(source|deployment)$"),
        issue_number: int = Query(ge=1),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(
                await operations(request).r3_issue_candidate(project, surface, issue_number)
            )
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/dashboard/findings")
    async def findings_api(
        request: Request,
        project: str | None = None,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_filters(project, None)
        return JSONResponse(await service(request).findings(project=project, limit=limit))

    @app.get("/api/dashboard/enhancements")
    async def enhancements_api(
        request: Request,
        project: str | None = None,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        _validate_filters(project, None)
        return JSONResponse(await service(request).enhancements(project=project, limit=limit))

    @app.post("/api/dashboard/actions/candidates")
    async def action_candidate_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            body = ActionCandidateInput.model_validate(await _json(request))
            if not _valid_csrf(authenticated, body.csrf_token):
                return JSONResponse({"detail": "invalid request"}, status_code=403)
            command = parse_command(body.command)
            value = await operations(request).candidate(
                command, sessions.binding_digest(authenticated.token)
            )
            return JSONResponse(value.model_dump(mode="json"))
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except Exception:  # noqa: BLE001 - never reflect form or password values
            return JSONResponse({"detail": "unable to prepare action"}, status_code=400)

    @app.post("/api/dashboard/actions/reauthenticate")
    async def action_reauthenticate_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        address = _client_address(request)
        if login_limiter.blocked(f"reauth:{address}"):
            return JSONResponse({"detail": "unable to authorize action"}, status_code=429)
        try:
            body = ActionReauthenticationInput.model_validate(await _json(request))
            if not _valid_csrf(authenticated, body.csrf_token):
                return JSONResponse({"detail": "invalid request"}, status_code=403)
            version = await repo(request).verify_password(body.current_password)
            if version != authenticated.data.password_version:
                login_limiter.failure(f"reauth:{address}")
                return JSONResponse({"detail": "unable to authorize action"}, status_code=401)
            token = await operations(request).create_reauthentication(
                body.candidate_id, sessions.binding_digest(authenticated.token)
            )
            login_limiter.success(f"reauth:{address}")
            return JSONResponse({"reauthentication_token": token, "expires_in": 300})
        except Exception:  # noqa: BLE001 - never reflect password values
            return JSONResponse({"detail": "unable to authorize action"}, status_code=400)

    @app.post("/api/dashboard/actions/execute", status_code=202)
    async def action_execute_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            body = ActionExecuteInput.model_validate(await _json(request))
            if not _valid_csrf(authenticated, body.csrf_token):
                return JSONResponse({"detail": "invalid request"}, status_code=403)
            value = await operations(request).queue_candidate(
                body.candidate_id,
                body.reauthentication_token,
                sessions.binding_digest(authenticated.token),
                body.confirmation_phrase,
            )
            return JSONResponse(value.model_dump(mode="json"), status_code=202)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except Exception:  # noqa: BLE001 - safe action boundary
            return JSONResponse({"detail": "unable to execute action"}, status_code=400)

    @app.get("/api/dashboard/operations/{operation_id}")
    async def dashboard_operation_api(operation_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        value = await operations(request).operation(operation_id)
        if value is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return JSONResponse(value.model_dump(mode="json"))

    @app.get("/api/dashboard/operations/{operation_id}/export")
    async def dashboard_operation_export_api(operation_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        value = await operations(request).export_content(operation_id)
        if value is None:
            raise HTTPException(status_code=404, detail="operation export not found")
        payload = (
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        return Response(
            payload,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="engineering-control-export-{operation_id}.json"'
                )
            },
        )

    @app.get("/api/dashboard/connections")
    async def connections_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        items = await operations(request).connections()
        return JSONResponse({"items": [item.model_dump(mode="json") for item in items]})

    @app.get("/api/dashboard/activity/recent")
    async def recent_activity(
        request: Request, limit: int = Query(default=500, ge=1, le=500)
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        items = await repo(request).recent_events(limit=limit)
        return JSONResponse({"items": items, "last_event_id": int(items[-1]["id"]) if items else 0})

    @app.get("/api/dashboard/activity/stream")
    async def activity_stream(request: Request, after_id: int = Query(default=0, ge=0)) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        raw_id = request.headers.get("last-event-id", str(after_id))
        try:
            after_id = max(0, int(raw_id))
        except ValueError:
            after_id = 0
        response = StreamingResponse(
            sse_events(
                repo(request),
                after_id=after_id,
                disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.get("/static/dashboard.css")
    async def dashboard_css(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return FileResponse(STATIC_ROOT / "dashboard.css", media_type="text/css")

    @app.get("/static/dashboard.js")
    async def dashboard_js(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return FileResponse(STATIC_ROOT / "dashboard.js", media_type="text/javascript")

    @app.exception_handler(404)
    async def not_found(request: Request, _error: Exception) -> Response:
        authenticated = await current_session(request)
        if authenticated is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context=page_context(
                request, authenticated, title="Not found", active="", error="Page not found."
            ),
            status_code=404,
        )

    return app


async def _form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="unsupported form encoding")
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=413, detail="form is too large")
    try:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="invalid form") from error
    if any(len(items) != 1 for items in values.values()):
        raise HTTPException(status_code=400, detail="duplicate form field")
    return {key: items[0] for key, items in values.items()}


async def _json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="unsupported JSON encoding")
    body = await request.body()
    if len(body) > 32_768:
        raise HTTPException(status_code=413, detail="request is too large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="invalid JSON") from error
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return value


def _set_cookie(response: Response, name: str, value: str, *, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _valid_csrf(authenticated: AuthenticatedSession, supplied: str | None) -> bool:
    return supplied is not None and hmac.compare_digest(authenticated.data.csrf_token, supplied)


def _safe_next(value: str | None) -> str:
    if value in {
        "/overview",
        "/backlog",
        "/workflows",
        "/activity",
        "/logs",
        "/audits",
        "/commands",
        "/connections",
        "/settings",
    }:
        return value
    return "/overview"


def _client_address(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    try:
        direct_ip = ipaddress.ip_address(direct)
    except ValueError:
        return direct
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if direct_ip.is_loopback and forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return direct
    return direct


def _validate_filters(project: str | None, status: str | None) -> None:
    if project is not None and project not in PROJECTS:
        raise HTTPException(status_code=422, detail="invalid project filter")
    if status is not None and status not in NORMALIZED_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status filter")


def _validate_observability_filters(
    project: str | None, status: str | None, provider: str | None
) -> None:
    _validate_filters(project, status)
    if provider is not None and provider not in {"codex", "claude"}:
        raise HTTPException(status_code=422, detail="invalid provider filter")


async def _record_safely(repository: DashboardRepository, event: ActivityEventInput) -> None:
    try:
        await repository.record_event(event)
    except Exception:  # noqa: BLE001 - event projection cannot change dashboard behavior
        return


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    run()
