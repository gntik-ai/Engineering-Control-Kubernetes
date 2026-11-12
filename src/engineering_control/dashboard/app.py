from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from engineering_control.config import Settings, get_settings
from engineering_control.dashboard.admission import AdmissionReader
from engineering_control.dashboard.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    LoginFailureLimiter,
    SignedSessionManager,
    read_session_key,
)
from engineering_control.dashboard.dependencies import AuthenticatedSession
from engineering_control.dashboard.events import sse_events, transcript_sse
from engineering_control.dashboard.project_records import (
    NAME as PROJECT_NAME,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    clean_repository,
)
from engineering_control.dashboard.project_sections import Guide
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.schemas import (
    ActionCandidateInput,
    ActionExecuteInput,
    ActionReauthenticationInput,
    ActivityEventInput,
    ProjectArchiveInput,
    ProjectCreateInput,
    ProjectRenameInput,
    R3CandidateCreateInput,
    SectionCreateInput,
    SectionFlowInput,
    SectionGuideInput,
    SectionRemoveInput,
    SectionUpdateInput,
)
from engineering_control.dashboard.service import DashboardService
from engineering_control.dashboard.session_store import SessionAuthorization
from engineering_control.dashboard.status_mapping import NORMALIZED_STATUSES
from engineering_control.dashboard.streaming import RevocableStreamingResponse, StreamAuthorization
from engineering_control.flow_library import flow_catalogue
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.logging import configure_logging
from engineering_control.operations.models import parse_command
from engineering_control.operations.registry import COMMAND_REGISTRY
from engineering_control.operations.repository import OperationsRepository
from engineering_control.projects import PROJECT_SET

PACKAGE_ROOT = Path(__file__).parent
# The built gntik-ui console (web/console, `pnpm build`): index.html plus hashed assets.
# Kept outside static/: that directory holds the hand-written, remote-free dashboard files.
CONSOLE_ROOT = PACKAGE_ROOT / "console"
CONSOLE_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
# The one inline <style> the console's select popup (Base UI, under gntik-ui) adds to the
# page: it hides the popup list's scrollbar. Only this exact text is allowed, and only on
# /console; if a kit upgrade changes it, the browser blocks the new text (the scrollbar
# shows) until the constant is updated. tests/test_dashboard_console.py checks the bundle.
CONSOLE_INLINE_STYLE = (
    ".base-ui-disable-scrollbar{scrollbar-width:none}"
    ".base-ui-disable-scrollbar::-webkit-scrollbar{display:none}"
)
CONSOLE_INLINE_STYLE_HASH = (
    "sha256-" + base64.b64encode(hashlib.sha256(CONSOLE_INLINE_STYLE.encode()).digest()).decode()
)
CONSOLE_VIEWS = frozenset({"", "projects", "project", "backlog", "executions", "execution"})
CONSOLE_FONT_SUFFIXES = frozenset({".woff", ".woff2"})
CONSOLE_ASSET_REFERENCE = re.compile(r'(?:src|href)="/console/assets/([^"]+)"')
CONSOLE_ASSET_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
PROJECTS = PROJECT_SET
LOGGER = logging.getLogger(__name__)


def sign_in_assets() -> frozenset[str]:
    """The console files given out without a session: the ones the sign-in page names.

    The sign-in page is built as its own entry (web/console/login.html), so this is its
    script, the stylesheet and the libraries. The console's own code is not among them.
    """
    page = CONSOLE_ROOT / "login.html"
    if not page.is_file():
        return frozenset()
    return frozenset(CONSOLE_ASSET_REFERENCE.findall(page.read_text(encoding="utf-8")))


def create_app(
    *,
    settings: Settings | None = None,
    repository: DashboardRepository | None = None,
    session_key: bytes | None = None,
    limiter: LoginFailureLimiter | None = None,
    operations_repository: OperationsRepository | None = None,
    admission_reader: AdmissionReader | None = None,
    github_app: GitHubApp | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    key = session_key
    if key is None:
        key = read_session_key(resolved.dashboard_session_key_file)
    sessions = SignedSessionManager(
        key,
        lifetime_seconds=resolved.dashboard_session_hours * 3600,
    )
    failure_log_next: dict[str, float] = {}
    failure_log_suppressed: dict[str, int] = {}
    active_failures: set[str] = set()
    failure_totals = {
        "health-readiness": 0,
        "logout-revocation": 0,
        "session-validation": 0,
    }

    def log_failure(key: str, message: str, error: BaseException) -> None:
        active_failures.add(key)
        failure_totals[key] += 1
        now = time.monotonic()
        if now < failure_log_next.get(key, 0.0):
            failure_log_suppressed[key] = failure_log_suppressed.get(key, 0) + 1
            return
        suppressed = failure_log_suppressed.pop(key, 0)
        failure_log_next[key] = now + 60.0
        LOGGER.error(
            "%s; error_type=%s; suppressed_since_last=%d",
            message,
            type(error).__name__,
            suppressed,
        )

    def log_recovery(key: str, message: str) -> None:
        if key not in active_failures:
            return
        active_failures.discard(key)
        suppressed = failure_log_suppressed.pop(key, 0)
        failure_log_next.pop(key, None)
        LOGGER.info("%s; suppressed_failures=%d", message, suppressed)

    login_limiter = limiter or LoginFailureLimiter()
    public_assets = sign_in_assets()
    trusted_proxies = tuple(
        ipaddress.ip_network(value) for value in resolved.dashboard_trusted_proxy_networks
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_repository = repository or DashboardRepository(
            resolved.database_url.get_secret_value()
        )
        app.state.dashboard_repository = resolved_repository
        resolved_admission = admission_reader
        # Injected fixture repositories must never fall back to live networking.
        if resolved_admission is None and repository is None:
            try:
                resolved_admission = AdmissionReader(
                    resolved.api_host, resolved.api_port, service=resolved.api_service_name
                )
            except ValueError:
                LOGGER.warning("dashboard API admission observation is unavailable")
        app.state.dashboard_service = DashboardService(resolved_repository, resolved_admission)
        try:
            # The projects the workflows know by name are records too; a release that adds
            # one to the registry gets its record here.
            await app.state.dashboard_service.sync_registry_projects()
        except Exception as error:  # noqa: BLE001 - the console must start without it
            log_failure("project-registry", "dashboard project registry sync failed", error)
        resolved_operations = operations_repository or OperationsRepository(
            resolved.database_url.get_secret_value(),
            export_retention_days=resolved.dashboard_operation_export_retention_days,
            export_max_bytes=resolved.dashboard_operation_export_max_bytes,
            project_config_root=resolved.project_config_root,
            projection_reconciliation_enabled=resolved.projection_reconciliation_enabled,
            projection_reconciliation_interval_seconds=resolved.projection_reconciliation_interval_seconds,
            projection_reconciliation_cycle_seconds=resolved.projection_reconciliation_cycle_seconds,
            projection_reconciliation_max_sweep_age_seconds=resolved.projection_reconciliation_max_sweep_age_seconds,
        )
        app.state.operations_repository = resolved_operations
        resolved_github = github_app
        # As above: a fixture repository never reaches out to GitHub by itself.
        if resolved_github is None and repository is None:
            resolved_github = GitHubApp.from_settings(resolved)
        app.state.github_app = resolved_github
        yield
        if github_app is None and resolved_github is not None:
            await resolved_github.aclose()
        if admission_reader is None and resolved_admission is not None:
            await resolved_admission.aclose()
        if repository is None:
            resolved_repository.engine.dispose()
        if operations_repository is None:
            resolved_operations.engine.dispose()

    app = FastAPI(
        title="Engineering Control Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        host = _request_host(request.headers.get("host"))
        response: Response
        if host not in resolved.dashboard_allowed_hosts:
            response = JSONResponse({"detail": "invalid request host"}, status_code=400)
        elif (
            request.url.path.startswith("/api/dashboard/")
            and await current_session(request) is None
        ):
            response = JSONResponse({"detail": "authentication required"}, status_code=401)
        elif request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not _trusted_origin(
            request, resolved.dashboard_allowed_origins
        ):
            response = JSONResponse({"detail": "invalid request origin"}, status_code=403)
        else:
            response = cast(Response, await call_next(request))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=()"
        )
        style_sources = "'self'"
        if request.url.path == "/console" or request.url.path.startswith("/console/"):
            style_sources = f"'self' '{CONSOLE_INLINE_STYLE_HASH}'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
            f"object-src 'none'; img-src 'self'; script-src 'self'; style-src {style_sources}; "
            "connect-src 'self'"
        )
        return response

    def repo(request: Request) -> DashboardRepository:
        return cast(DashboardRepository, request.app.state.dashboard_repository)

    def service(request: Request) -> DashboardService:
        return cast(DashboardService, request.app.state.dashboard_service)

    def operations(request: Request) -> OperationsRepository:
        return cast(OperationsRepository, request.app.state.operations_repository)

    async def check_filters(
        request: Request, project: str | None, status: str | None, provider: str | None = None
    ) -> None:
        """A project filter names a project of the registry or an active record."""
        if (
            project is not None
            and project not in PROJECTS
            and not (
                PROJECT_NAME.fullmatch(project) and await service(request).project_exists(project)
            )
        ):
            raise HTTPException(status_code=422, detail="invalid project filter")
        _validate_observability_filters(None, status, provider)

    def session_authorization(authenticated: AuthenticatedSession) -> SessionAuthorization:
        return SessionAuthorization(
            sessions.binding_digest(authenticated.token),
            authenticated.data.password_version,
            datetime.fromtimestamp(authenticated.data.expires_at, UTC),
        )

    async def current_session(request: Request) -> AuthenticatedSession | None:
        if getattr(request.state, "dashboard_session_checked", False):
            return cast(AuthenticatedSession | None, request.state.dashboard_session)
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            request.state.dashboard_session_checked = True
            request.state.dashboard_session = None
            return None
        data = sessions.verify(token)
        if data is None:
            request.state.dashboard_session_checked = True
            request.state.dashboard_session = None
            return None
        try:
            authorized = await repo(request).session_authorized(
                sessions.binding_digest(token),
                password_version=data.password_version,
            )
        except Exception as error:  # noqa: BLE001 - authentication must fail closed on DB errors
            log_failure("session-validation", "dashboard session validation failed", error)
            request.state.dashboard_session_checked = True
            request.state.dashboard_session = None
            return None
        log_recovery("session-validation", "dashboard session validation recovered")
        if not authorized:
            request.state.dashboard_session_checked = True
            request.state.dashboard_session = None
            return None
        authenticated = AuthenticatedSession(token=token, data=data)
        request.state.dashboard_session_checked = True
        request.state.dashboard_session = authenticated
        return authenticated

    async def request_authorized(request: Request) -> bool:
        request.state.dashboard_session_checked = False
        request.state.dashboard_session = None
        return await current_session(request) is not None

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

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(request: Request) -> Response:
        try:
            repository_ready = await repo(request).ready()
        except Exception as error:  # noqa: BLE001 - readiness fails closed without database detail
            log_failure("health-readiness", "dashboard readiness persistence check failed", error)
            return JSONResponse({"status": "not-ready"}, status_code=503)
        log_recovery("health-readiness", "dashboard readiness persistence check recovered")
        # This query touches the same authentication and revocation relations
        # as session validation, including after all browser cookies are gone.
        log_recovery("session-validation", "dashboard session validation recovered")
        if not repository_ready:
            return JSONResponse({"status": "not-ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/internal/metrics")
    async def internal_metrics(request: Request) -> Response:
        if _request_host(request.headers.get("host")) not in {"127.0.0.1", "localhost", "::1"}:
            return Response(status_code=404)
        labels = {
            "health-readiness": "readiness",
            "logout-revocation": "logout_revocation",
            "session-validation": "session_validation",
        }
        lines = [
            "# HELP engineering_dashboard_persistence_failures_total "
            "Fixed-boundary persistence failures.",
            "# TYPE engineering_dashboard_persistence_failures_total counter",
        ]
        lines.extend(
            "engineering_dashboard_persistence_failures_total"
            f'{{operation="{label}"}} {failure_totals[key]}'
            for key, label in labels.items()
        )
        lines.extend(
            [
                "# HELP engineering_dashboard_persistence_failure_active "
                "One while the fixed persistence boundary has not recovered.",
                "# TYPE engineering_dashboard_persistence_failure_active gauge",
            ]
        )
        lines.extend(
            "engineering_dashboard_persistence_failure_active"
            f'{{operation="{label}"}} {int(key in active_failures)}'
            for key, label in labels.items()
        )
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/robots.txt")
    async def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        """The sign-in page of the gntik-ui console. It holds no token; see the challenge."""
        if await current_session(request) is not None:
            return RedirectResponse("/console", status_code=303)
        page = CONSOLE_ROOT / "login.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="page not found")
        return FileResponse(page, media_type="text/html")

    @app.get("/login/challenge")
    async def login_challenge(request: Request) -> Response:
        """The token a sign-in has to send back, also set as a cookie the page cannot read."""
        csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        if not csrf or not sessions.verify_login_csrf(csrf):
            csrf = sessions.create_login_csrf()
        response = JSONResponse({"csrf_token": csrf})
        _set_cookie(response, LOGIN_CSRF_COOKIE, csrf, max_age=900)
        return response

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await _form(request)
        csrf = form.get("csrf_token", "")
        csrf_cookie = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        next_path = _safe_next(form.get("next_path"))
        address = _client_address(request, trusted_proxies)
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
            response = JSONResponse(
                {"detail": "Unable to sign in.", "csrf_token": fresh_csrf},
                status_code=429 if blocked else 401,
            )
            _set_cookie(response, LOGIN_CSRF_COOKIE, fresh_csrf, max_age=900)
            return response
        token = sessions.create(version)
        previous_token = request.cookies.get(SESSION_COOKIE, "")
        previous_data = sessions.verify(previous_token) if previous_token else None
        previous = (
            session_authorization(AuthenticatedSession(token=previous_token, data=previous_data))
            if previous_data is not None
            else None
        )
        try:
            await repo(request).confirm_login(version, previous)
        except Exception as error:  # noqa: BLE001 - never issue a cookie on uncertain rotation
            log_failure("session-validation", "dashboard login rotation failed", error)
            return JSONResponse(
                {"detail": "Sign-in is temporarily unavailable; try again."}, status_code=503
            )
        login_limiter.success(address)
        redirect = RedirectResponse(next_path, status_code=303)
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
        now = datetime.now(UTC)
        expires_at = min(
            datetime.fromtimestamp(authenticated.data.expires_at, UTC),
            now + timedelta(seconds=sessions.lifetime_seconds),
        )
        try:
            await repo(request).revoke_session(
                sessions.binding_digest(authenticated.token),
                password_version=authenticated.data.password_version,
                expires_at=expires_at,
            )
        except Exception as error:  # noqa: BLE001 - logout must fail closed without database detail
            log_failure(
                "logout-revocation",
                "dashboard logout session revocation failed",
                error,
            )
            return PlainTextResponse(
                "Logout is temporarily unavailable; this session remains active.",
                status_code=503,
            )
        log_recovery("logout-revocation", "dashboard logout session revocation recovered")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/")
    async def root(request: Request) -> Response:
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        return RedirectResponse("/console", status_code=303)

    @app.get("/api/dashboard/session")
    async def session_api(request: Request) -> Response:
        """What the console needs to sign out: the session's CSRF token and its expiry."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(
            {
                "csrf_token": authenticated.data.csrf_token,
                "expires_at": datetime.fromtimestamp(
                    authenticated.data.expires_at, UTC
                ).isoformat(),
            }
        )

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
        await check_filters(request, project, status)
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
        await check_filters(request, project, status, provider)
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
        await check_filters(request, project, status, provider)
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
        stream_authorization = StreamAuthorization(lambda: request_authorized(request))
        response = RevocableStreamingResponse(
            transcript_sse(
                repo(request).observability,
                execution_id,
                after_sequence=after_sequence,
                disconnected=request.is_disconnected,
                authorized=stream_authorization.refresh,
            ),
            stream_authorization,
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

    @app.get("/api/dashboard/projects")
    async def projects_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).projects())

    async def project_change(
        request: Request,
        model: type[Any],
        change: Any,
        *,
        event: str,
        subject: str | None = None,
        status_code: int = 200,
    ) -> Response:
        """One shape for every change to a project: session, body, CSRF, change, record."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            body = model.model_validate(await _json(request))
        except ValueError:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        if not _valid_csrf(authenticated, body.csrf_token):
            return JSONResponse({"detail": "invalid request"}, status_code=403)
        try:
            changed = await change(body)
        except ProjectInvalid as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        except ProjectConflict as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        except GitHubAppError as error:
            return JSONResponse({"detail": str(error)}, status_code=502)
        # What changed: a project by its name, or a section as project/section.
        label = subject or str(changed["project"])
        action = event.rsplit(".", 1)[-1].replace("_", " ")
        await _record_safely(
            repo(request),
            ActivityEventInput(
                level="INFO",
                event_type=event,
                source="dashboard",
                project="platform",
                entity_type="project",
                entity_id=label[:240],
                message=f"Project {label}: {action}",
            ),
        )
        return JSONResponse(changed, status_code=status_code)

    def github(request: Request) -> GitHubApp | None:
        return cast(GitHubApp | None, request.app.state.github_app)

    @app.get("/api/dashboard/github/repositories")
    async def github_repositories_api(request: Request) -> Response:
        """The repositories the GitHub App is installed on, and the project each belongs to."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        installed = github(request)
        if installed is None:
            return JSONResponse({"configured": False, "items": []})
        try:
            repositories = await installed.repositories()
        except GitHubAppError as error:
            return JSONResponse({"detail": str(error)}, status_code=502)
        connected = await service(request).connected_repositories()
        return JSONResponse(
            {
                "configured": True,
                "items": [
                    {**item.as_dict(), "project": connected.get(item.full_name.lower())}
                    for item in repositories
                ],
            }
        )

    @app.post("/api/dashboard/projects", status_code=201)
    async def create_project_api(request: Request) -> Response:
        async def create(body: ProjectCreateInput) -> dict[str, object]:
            repository = clean_repository(body.repository)
            default_branch = "main"
            installed = github(request)
            if installed is not None:
                # With the app configured, a project is connected only to a repository the
                # app can work on, under the name and default branch GitHub gives it.
                found = await installed.repository(repository)
                if found is None:
                    raise ProjectInvalid(f"The GitHub App is not installed on {repository}")
                if found.archived:
                    raise ProjectInvalid(f"The repository {found.full_name} is archived on GitHub")
                repository, default_branch = found.full_name, found.default_branch
            return await service(request).create_project(
                name=body.name,
                display_name=body.display_name,
                repository=repository,
                default_branch=default_branch,
            )

        return await project_change(
            request,
            ProjectCreateInput,
            create,
            event="dashboard.project_created",
            status_code=201,
        )

    @app.post("/api/dashboard/projects/{name}/rename")
    async def rename_project_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ProjectRenameInput,
            lambda body: service(request).rename_project(name, body.display_name),
            event="dashboard.project_renamed",
        )

    @app.get("/api/dashboard/flows")
    async def flows_api(request: Request) -> Response:
        """The flows a section can be assigned."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse({"items": flow_catalogue()})

    @app.get("/api/dashboard/projects/{name}")
    async def project_api(name: str, request: Request) -> Response:
        """One project: what the list shows for it, and its sections."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        found = await service(request).project(name) if PROJECT_NAME.fullmatch(name) else None
        if found is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse(found)

    @app.get("/api/dashboard/projects/{name}/routing")
    async def project_routing_api(name: str, request: Request) -> Response:
        """What the supervisor is given to decide which section a piece of work belongs to."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            brief = await service(request).routing_brief(name)
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse({"project": name, "brief": brief})

    def guide(body: SectionGuideInput) -> Guide:
        return Guide(
            description=body.description,
            use_when=body.use_when,
            avoid_when=body.avoid_when,
            labels=body.labels,
            examples=body.examples,
        )

    @app.post("/api/dashboard/projects/{name}/sections", status_code=201)
    async def add_section_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionCreateInput,
            lambda body: service(request).add_section(name, name=body.name, guide=guide(body)),
            event="dashboard.section_added",
            subject=name,
            status_code=201,
        )

    @app.post("/api/dashboard/projects/{name}/sections/{key}/update")
    async def update_section_api(name: str, key: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionUpdateInput,
            lambda body: service(request).update_section(
                name, key, name=body.name, guide=guide(body)
            ),
            event="dashboard.section_changed",
            subject=f"{name}/{key}",
        )

    @app.post("/api/dashboard/projects/{name}/sections/{key}/remove")
    async def remove_section_api(name: str, key: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionRemoveInput,
            lambda _body: service(request).remove_section(name, key),
            event="dashboard.section_removed",
            subject=f"{name}/{key}",
        )

    @app.post("/api/dashboard/projects/{name}/sections/{key}/flow")
    async def assign_flow_api(name: str, key: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionFlowInput,
            lambda body: service(request).assign_flow(name, key, body.flow),
            event="dashboard.section_flow_assigned",
            subject=f"{name}/{key}",
        )

    @app.post("/api/dashboard/projects/{name}/archive")
    async def archive_project_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ProjectArchiveInput,
            lambda _body: service(request).archive_project(name),
            event="dashboard.project_removed",
        )

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
        await check_filters(request, project, None)
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
        await check_filters(request, project, status)
        return JSONResponse(await service(request).audits(project=project, status=status))

    @app.get("/api/dashboard/commands")
    async def commands_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse({"items": COMMAND_REGISTRY})

    @app.get("/api/dashboard/r3-issues/candidate")
    async def dashboard_r3_issue_candidate_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            project, surface, issue_number = _r3_candidate_target(request)
        except ValueError:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        try:
            return JSONResponse(
                await operations(request).r3_issue_candidate_status(project, surface, issue_number)
            )
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=404)

    @app.post("/api/dashboard/r3-issues/candidate", status_code=201)
    async def dashboard_r3_issue_candidate_create_api(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            project, surface, issue_number = _r3_candidate_target(request)
        except ValueError:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        body = await _json(request)
        try:
            candidate = R3CandidateCreateInput.model_validate(body)
        except ValueError:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        if not _valid_csrf(authenticated, candidate.csrf_token):
            return JSONResponse({"detail": "invalid request"}, status_code=403)
        try:
            return JSONResponse(
                await operations(request).r3_issue_candidate(project, surface, issue_number),
                status_code=201,
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
        await check_filters(request, project, None)
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
        await check_filters(request, project, None)
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
        address = _client_address(request, trusted_proxies)
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
            replacement = sessions.create(version)
            replacement_data = sessions.verify(replacement)
            if replacement_data is None:
                raise ValueError("replacement session is invalid")
            token, expires_at = await operations(request).rotate_reauthentication(
                body.candidate_id,
                session_authorization(authenticated),
                sessions.binding_digest(replacement),
            )
            login_limiter.success(f"reauth:{address}")
            response = JSONResponse(
                {
                    "reauthentication_token": token,
                    "expires_in": max(0, int((expires_at - datetime.now(UTC)).total_seconds())),
                    "expires_at": expires_at.isoformat(),
                    "csrf_token": replacement_data.csrf_token,
                    "session_rotated": True,
                }
            )
            _set_cookie(
                response,
                SESSION_COOKIE,
                replacement,
                max_age=resolved.dashboard_session_hours * 3600,
            )
            return response
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
                authorization=session_authorization(authenticated),
            )
            return JSONResponse(
                await service(request).operation_response(value.model_dump(mode="json")),
                status_code=202,
            )
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
        return JSONResponse(
            await service(request).operation_response(value.model_dump(mode="json"))
        )

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
        stream_authorization = StreamAuthorization(lambda: request_authorized(request))
        response = RevocableStreamingResponse(
            sse_events(
                repo(request),
                after_id=after_id,
                disconnected=request.is_disconnected,
                authorized=stream_authorization.refresh,
            ),
            stream_authorization,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.get("/console/assets/{name}")
    async def console_asset(name: str, request: Request) -> Response:
        # The sign-in page's files and the fonts are public; the rest needs a session.
        if name not in public_assets and Path(name).suffix not in CONSOLE_FONT_SUFFIXES:
            authenticated = await require_session(request)
            if isinstance(authenticated, Response):
                return authenticated
        media_type = CONSOLE_ASSET_TYPES.get(Path(name).suffix)
        path = CONSOLE_ROOT / "assets" / name
        if media_type is None or not CONSOLE_ASSET_NAME.fullmatch(name) or not path.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(path, media_type=media_type)

    @app.get("/console")
    @app.get("/console/{view}")
    async def console_page(request: Request, view: str = "") -> Response:
        """The gntik-ui console: one client-routed page over the read-only dashboard API."""
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        index = CONSOLE_ROOT / "index.html"
        if view not in CONSOLE_VIEWS or not index.is_file():
            raise HTTPException(status_code=404, detail="page not found")
        return FileResponse(index, media_type="text/html")

    @app.exception_handler(404)
    async def not_found(request: Request, _error: Exception) -> Response:
        if await current_session(request) is None:
            return RedirectResponse("/login", status_code=303)
        index = CONSOLE_ROOT / "index.html"
        path = request.url.path
        if (
            request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
            and not path.startswith(("/api/", "/console/assets/"))
            and index.is_file()
        ):
            # A browser asking for a page gets the console, which shows its not-found page.
            return FileResponse(index, media_type="text/html", status_code=404)
        return JSONResponse({"detail": "not found"}, status_code=404)

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
    if value is not None and value.startswith("/console/"):
        view = value.removeprefix("/console/")
        if view and view in CONSOLE_VIEWS:
            return value
    return "/console"


def _client_address(
    request: Request,
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
) -> str:
    """The address sign-in attempts are counted against.

    The forwarded address is believed only when the connection comes from a proxy: the
    loopback interface, or a network named in the settings. The entry used is the last
    one, the one the nearest proxy wrote; earlier entries are whatever the client sent.
    """
    direct = request.client.host if request.client else "unknown"
    try:
        direct_ip = ipaddress.ip_address(direct)
    except ValueError:
        return direct
    if not direct_ip.is_loopback and not any(direct_ip in network for network in trusted_proxies):
        return direct
    forwarded = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return direct


def _request_host(value: str | None) -> str:
    if not value:
        return ""
    if value != value.strip() or any(character in value for character in "/\\@?#"):
        return ""
    try:
        parsed = urlsplit(f"//{value}")
        if parsed.username is not None or parsed.password is not None:
            return ""
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _trusted_origin(request: Request, allowed: tuple[str, ...]) -> bool:
    supplied = request.headers.get("origin")
    if supplied:
        # Chromium serializes the Origin of a same-origin form navigation as
        # ``null`` when the response has Referrer-Policy: no-referrer.  Keep
        # rejecting opaque cross-site and programmatic requests, but allow the
        # browser's narrowly attested same-origin document navigation.  The
        # middleware's preceding exact Host allowlist remains the canonical
        # target binding, and CSRF validation still runs independently in each
        # form handler.  Fetch Metadata is defense in depth, not authentication:
        # a non-browser client can forge it but cannot derive the CSRF secret.
        if supplied == "null":
            return (
                request.headers.get("sec-fetch-site") == "same-origin"
                and request.headers.get("sec-fetch-mode") == "navigate"
                and request.headers.get("sec-fetch-dest") == "document"
            )
        return _canonical_origin(supplied, allow_path=False) in allowed
    referer = request.headers.get("referer")
    if not referer:
        return False
    return _canonical_origin(referer, allow_path=True) in allowed


def _canonical_origin(value: str, *, allow_path: bool) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or (
                not allow_path and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment)
            )
        ):
            return ""
        origin = f"{parsed.scheme.lower()}://{parsed.hostname.lower()}"
        if parsed.port is not None:
            origin = f"{origin}:{parsed.port}"
        return origin
    except ValueError:
        return ""


def _validate_filters(project: str | None, status: str | None) -> None:
    if project is not None and project not in PROJECTS:
        raise HTTPException(status_code=422, detail="invalid project filter")
    if status is not None and status not in NORMALIZED_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status filter")


def _r3_candidate_target(request: Request) -> tuple[str, str, int]:
    """Validate the fixed R3 lookup only after the caller is authenticated."""
    if len(request.url.query) > 256:
        raise ValueError("invalid R3 candidate target")
    items = list(request.query_params.multi_items())
    if len(items) != 3 or {key for key, _value in items} != {
        "project",
        "surface",
        "issue_number",
    }:
        raise ValueError("invalid R3 candidate target")
    project = request.query_params.get("project")
    surface = request.query_params.get("surface")
    issue_number = request.query_params.get("issue_number")
    if project != "musematic" or surface != "source" or issue_number != "1119":
        raise ValueError("invalid R3 candidate target")
    return project, surface, 1119


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
