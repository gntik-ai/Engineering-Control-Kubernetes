from __future__ import annotations

import asyncio
import base64
import contextlib
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
from typing import Any, Final, Literal, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from engineering_control.cluster import Cluster, ClusterError
from engineering_control.config import Settings, get_settings
from engineering_control.dashboard import (
    evidence_verdicts,
    review_budget,
    review_evidence,
    review_flows,
    stats,
)
from engineering_control.dashboard.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    LoginFailureLimiter,
    SignedSessionManager,
    read_session_key,
)
from engineering_control.dashboard.backlog_items import STATES as BACKLOG_STATES
from engineering_control.dashboard.bring_over import SCRIPT as EXPORT_SCRIPT
from engineering_control.dashboard.bring_over import export_script
from engineering_control.dashboard.dependencies import AuthenticatedSession
from engineering_control.dashboard.planning import IssueView, Kind, PlanningState
from engineering_control.dashboard.project_records import (
    NAME as PROJECT_NAME,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    clean_repository,
)
from engineering_control.dashboard.project_sections import SECTION_KEY, Guide
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.schemas import (
    AccountCreateInput,
    AccountRemoveInput,
    AccountSignInInput,
    AccountUpdateInput,
    ActivityEventInput,
    AgentInput,
    BacklogSyncInput,
    BudgetInput,
    CacheTrialInput,
    CommandProfileInput,
    CommandSelectionInput,
    ConnectionAnalysisInput,
    ConnectionBulkLinksInput,
    ConnectionDecisionInput,
    ConnectionInput,
    ConnectionLinkInput,
    EpicInput,
    EpicQueueInput,
    EvaluationInput,
    EvidenceControlInput,
    EvidenceDecisionInput,
    EvidencePreviewInput,
    EvidenceRunInput,
    ExecutionPlanInput,
    FlowCheckInput,
    FlowCreateInput,
    FlowDraftInput,
    FlowPublishInput,
    FlowSimulationInput,
    ItemBulkInput,
    ItemCreateInput,
    ItemEditInput,
    ItemRouteInput,
    ItemStateInput,
    ManagedCoverageInput,
    PlainChangeInput,
    PlanningConfirmation,
    PlanningStateInput,
    ProjectApplicationInput,
    ProjectArchiveInput,
    ProjectCommandsInput,
    ProjectCreateInput,
    ProjectDescriptionInput,
    ProjectPauseInput,
    ProjectRenameInput,
    ProjectSupervisorMessage,
    ProposalDecisions,
    ProposalEdit,
    QueueCreateInput,
    QueueItemsInput,
    QueueUpdateInput,
    RecordPauseInput,
    RecordsApplyInput,
    RecordsInput,
    ReviewBudgetInput,
    ReviewPauseInput,
    RunDecisionInput,
    RunsRemoveInput,
    SavedTaskInput,
    SectionCreateInput,
    SectionFlowInput,
    SectionGuideInput,
    SectionRemoveInput,
    SectionRunInput,
    SectionUpdateInput,
    SkillInput,
    StoryInput,
    SummariesInput,
    SupervisorDecisionInput,
    SupervisorDefinitionInput,
    SupervisorMessageInput,
    SupervisorModelInput,
    SupervisorStopInput,
    TaskHiddenInput,
    TaskInput,
    TriggerInput,
    TriggerUpdateInput,
    WorkspaceControlInput,
    WorkspaceDescription,
    WorkspaceImportStep,
    WorkspaceIssueInput,
    WorkspaceIssueStories,
    WorkspaceReviewInput,
)
from engineering_control.dashboard.service import DashboardService
from engineering_control.dashboard.session_store import SessionAuthorization
from engineering_control.dashboard.workspace_costs import ecb_rates
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.health import Health, listen
from engineering_control.logging import configure_logging
from engineering_control.models.sealing import read_sealing_key
from engineering_control.models.service import Models
from engineering_control.projects import PROJECT_SET
from engineering_control.supervisor import advice as supervisor_advice

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
CONSOLE_VIEWS = frozenset(
    {
        "",
        "admin",
        "workspace",
        "inbox",
        "projects",
        "project",
        "backlog",
        "settings/accounts",
        "settings/stats",
        "settings/supervisor",
        "runs",
        "run",
        "supervisor",
        "queues",
        "budgets",
        "queue",
        "flows",
        "flow",
    }
)
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
    github_app: GitHubApp | None = None,
    cluster: Cluster | None = None,
    models: Models | None = None,
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
        "backlog-sync": 0,
        "project-registry": 0,
        "record-cleanup": 0,
    }
    health = Health("console")

    def log_failure(key: str, message: str, error: BaseException) -> None:
        active_failures.add(key)
        # A key that is not listed above is still counted: a failure is never an error
        # of its own.
        failure_totals[key] = failure_totals.get(key, 0) + 1
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

    async def keep_backlogs_in_step(
        backlog_service: DashboardService, installed: GitHubApp, *, interval_seconds: int
    ) -> None:
        """Every interval, import each project's open issues. One project failing (its
        repository gone, GitHub not answering) never stops the others or the console."""
        await asyncio.sleep(min(60, interval_seconds))
        while True:
            health.beat("backlog-sync")
            try:
                names = await backlog_service.active_project_names()
            except Exception as error:  # noqa: BLE001 - try again at the next interval
                log_failure("backlog-sync", "dashboard backlog sync could not list projects", error)
                names = []
            for name in names:
                try:
                    await backlog_service.sync_backlog(name, installed)
                    log_recovery("backlog-sync", "dashboard backlog sync recovered")
                except Exception as error:  # noqa: BLE001 - recorded on the project; go on
                    log_failure("backlog-sync", "dashboard backlog sync failed", error)
            await asyncio.sleep(interval_seconds)

    async def keep_the_record_short(kept: DashboardRepository) -> None:
        """Four times a day, drop what the record of the console holds beyond its time."""
        while True:
            try:
                await kept.delete_expired_events(
                    retention_days=resolved.dashboard_event_retention_days
                )
                log_recovery("record-cleanup", "dashboard record clean-up recovered")
            except Exception as error:  # noqa: BLE001 - tried again at the next turn
                log_failure("record-cleanup", "dashboard record clean-up failed", error)
            await asyncio.sleep(6 * 3600)

    def own_metrics() -> list[str]:
        """What the console counts about itself: failures at the edges it guards."""
        labels = {key: key.replace("-", "_").removeprefix("health_") for key in failure_totals}
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
        return lines

    async def told_to_the_cluster() -> list[str]:
        return own_metrics()

    async def publish_workspace_approvals(
        records: DashboardRepository, installed: GitHubApp
    ) -> None:
        while True:
            try:
                for name, key in await asyncio.to_thread(records.publishing.due):
                    try:
                        await records.publishing.sync(name, key, installed)
                    except (ProjectConflict, ProjectNotFound):
                        continue
            except Exception as error:  # noqa: BLE001 - the durable change remains for a retry
                log_failure("workspace-sync", "workspace publication retry failed", error)
            await asyncio.sleep(30)

    async def analyze_connections(records: DashboardRepository, models: Models | None) -> None:
        next_review_check = 0.0
        while True:
            try:
                if asyncio.get_running_loop().time() >= next_review_check:
                    await asyncio.to_thread(records.workspace_imports.settle_existing_reviews)
                    next_review_check = asyncio.get_running_loop().time() + 60
                worked = await records.connection_analysis.tick(models)
            except Exception as error:  # noqa: BLE001 - the durable lease makes a failed pass retryable
                log_failure("connection-review", "Project connection review could not run", error)
                worked = False
            await asyncio.sleep(0.2 if worked else 5)

    async def review_new_planning(records: DashboardRepository, available: Models) -> None:
        report_at = 0.0
        while True:
            try:
                if asyncio.get_running_loop().time() >= report_at:
                    report_at = asyncio.get_running_loop().time() + 900
                    LOGGER.info(
                        "Automatic review queues %s",
                        json.dumps(
                            await asyncio.to_thread(records.planning_analysis.active_counts)
                        ),
                    )
                planned, reviewed, coverage, advice = await asyncio.gather(
                    records.execution_plans.tick(available),
                    records.planning_analysis.tick(available),
                    records.workspace_imports.tick_coverage(available),
                    supervisor_advice.tick(records.sessions, available),
                )
                worked = planned or reviewed or coverage or advice
            except Exception as error:  # noqa: BLE001 - an expired lease is resumed on the next pass
                log_failure("planning-review", "Automatic planning review could not run", error)
                worked = False
            await asyncio.sleep(0.2 if worked else 5)

    async def keep_model_catalogs_current(available: Models) -> None:
        while True:
            try:
                result = await available.refresh_catalogs()
                LOGGER.info(
                    "Model catalogs refreshed=%d unavailable=%d",
                    result["refreshed"],
                    result["unavailable"],
                )
                log_recovery("model-catalogs", "model catalog refresh recovered")
            except Exception as error:  # noqa: BLE001 - existing model choices remain usable
                log_failure("model-catalogs", "model catalog refresh failed", error)
            await asyncio.sleep(24 * 3600)

    async def keep_exchange_rates_current(records: DashboardRepository) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                try:
                    fresh = await ecb_rates(client)
                    records.workspace_metrics.exchange_rates = fresh
                    log_recovery("exchange-rates", "ECB exchange rates refreshed")
                except Exception as error:  # noqa: BLE001 - serve the last available reference
                    log_failure(
                        "exchange-rates", "ECB exchange rates could not be refreshed", error
                    )
                await asyncio.sleep(6 * 3600)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_repository = repository or DashboardRepository(
            resolved.database_url.get_secret_value()
        )
        app.state.dashboard_repository = resolved_repository
        app.state.dashboard_service = DashboardService(resolved_repository)
        try:
            # The projects the original system worked on are records too; a release that
            # adds one to the registry gets its record here.
            await app.state.dashboard_service.sync_registry_projects()
        except Exception as error:  # noqa: BLE001 - the console must start without it
            log_failure("project-registry", "dashboard project registry sync failed", error)
        resolved_github = github_app
        # As above: a fixture repository never reaches out to GitHub by itself.
        if resolved_github is None and repository is None:
            resolved_github = GitHubApp.from_settings(resolved)
        app.state.github_app = resolved_github
        resolved_cluster = cluster
        # A fixture repository never reaches a cluster by itself either.
        if resolved_cluster is None and repository is None:
            resolved_cluster = Cluster.from_settings(resolved)
        app.state.cluster = resolved_cluster
        # The accounts with an API key: sealed here, and asked which models they reach.
        app.state.models = models or Models(
            resolved_repository.sessions, read_sealing_key(resolved.model_keys_key_file)
        )
        keepers: list[asyncio.Task[None]] = []
        if repository is None and resolved_github is not None:
            keepers.append(
                asyncio.create_task(
                    publish_workspace_approvals(resolved_repository, resolved_github)
                )
            )
        # Keep every project's backlog in step with GitHub, unless a fixture supplied the
        # app (a test drives the import itself) or the interval is zero.
        if github_app is None and resolved_github is not None and resolved.backlog_sync_minutes:
            interval = resolved.backlog_sync_minutes * 60
            # GitHub may be slow for every project in turn; three intervals without a
            # pass is a loop that stands.
            health.expect("backlog-sync", within=3 * interval + 600)
            keepers.append(
                asyncio.create_task(
                    keep_backlogs_in_step(
                        app.state.dashboard_service, resolved_github, interval_seconds=interval
                    )
                )
            )
        told: asyncio.Server | None = None
        # The process itself, not an app a test made: it says how it is doing to the
        # cluster, and keeps its own record within its time.
        if repository is None:
            keepers.append(
                asyncio.create_task(analyze_connections(resolved_repository, app.state.models))
            )
            keepers.append(
                asyncio.create_task(review_new_planning(resolved_repository, app.state.models))
            )
            keepers.append(
                asyncio.create_task(
                    review_evidence.worker(
                        resolved_repository.sessions,
                        app.state.models,
                        lambda: resolved_repository.workspace_metrics.exchange_rates,
                    )
                )
            )
            keepers.append(asyncio.create_task(keep_the_record_short(resolved_repository)))
            keepers.append(asyncio.create_task(keep_exchange_rates_current(resolved_repository)))
            if app.state.models.sealing_key is not None:
                keepers.append(asyncio.create_task(keep_model_catalogs_current(app.state.models)))
            told = await listen(
                health, resolved.health_host, resolved.health_port, extra=told_to_the_cluster
            )
        yield
        await resolved_repository.workspace_imports.stop_reviews()
        if told is not None:
            told.close()
        for keeper in keepers:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
        if github_app is None and resolved_github is not None:
            await resolved_github.aclose()
        if cluster is None and resolved_cluster is not None:
            await resolved_cluster.aclose()
        if models is None:
            await app.state.models.aclose()
        if repository is None:
            resolved_repository.engine.dispose()

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

    async def check_project(request: Request, project: str | None) -> None:
        """A project filter names a project of the registry or an active record."""
        if (
            project is not None
            and project not in PROJECTS
            and not (
                PROJECT_NAME.fullmatch(project) and await service(request).project_exists(project)
            )
        ):
            raise HTTPException(status_code=422, detail="invalid project filter")

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
        response = RedirectResponse("/login", status_code=303)
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
        lines = own_metrics()
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
        redirect = RedirectResponse("/console", status_code=303)
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
        password: bool = False,
        entity: str = "project",
        max_bytes: int = 32_768,
        record: bool = True,
    ) -> Response:
        """One shape for every change the administrator makes: session, body, CSRF, change,
        record. ``entity`` names what was changed, a project unless said otherwise."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            body = model.model_validate(await _json(request, max_bytes))
        except ValueError:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        if not _valid_csrf(authenticated, body.csrf_token):
            return JSONResponse({"detail": "invalid request"}, status_code=403)
        if password:
            # A change that removes something or touches many things at once is confirmed
            # with the password, counted against the same limit as a sign-in.
            address = f"reauth:{_client_address(request, trusted_proxies)}"
            if login_limiter.blocked(address):
                return JSONResponse(
                    {"detail": "Too many wrong passwords. Wait 15 minutes, then try again."},
                    status_code=429,
                )
            try:
                version = await repo(request).verify_password(body.current_password)
            except Exception as error:  # noqa: BLE001 - never reflect the password or the cause
                log_failure("session-validation", "dashboard password confirmation failed", error)
                return JSONResponse(
                    {"detail": "The password could not be checked; try again."}, status_code=503
                )
            if version != authenticated.data.password_version:
                login_limiter.failure(address)
                # 403, not 401: the session is good; it is this change that is refused.
                return JSONResponse({"detail": "That password was not accepted."}, status_code=403)
            login_limiter.success(address)
        try:
            changed = await change(body)
        except ProjectInvalid as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        except ProjectConflict as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        except (GitHubAppError, ClusterError) as error:
            return JSONResponse({"detail": str(error)}, status_code=502)
        if not record:
            # Something was only looked at: there is nothing to keep a record of.
            return JSONResponse(changed, status_code=status_code)
        # What changed: a project by its name, a section as project/section, an account.
        label = subject or str(changed[entity])
        action = event.rsplit(".", 1)[-1].replace("_", " ")
        await _record_safely(
            repo(request),
            ActivityEventInput(
                level="INFO",
                event_type=event,
                source="dashboard",
                project="platform",
                entity_type=entity,
                entity_id=label[:240],
                message=f"{entity.capitalize()} {label}: {action}",
            ),
        )
        return JSONResponse(changed, status_code=status_code)

    def github(request: Request) -> GitHubApp | None:
        return cast(GitHubApp | None, request.app.state.github_app)

    def cluster_of(request: Request) -> Cluster | None:
        return cast(Cluster | None, request.app.state.cluster)

    def models_of(request: Request) -> Models | None:
        return cast(Models | None, getattr(request.app.state, "models", None))

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
                description=body.description,
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

    @app.post("/api/dashboard/projects/{name}/description")
    async def describe_project_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ProjectDescriptionInput,
            lambda body: service(request).describe_project(name, body.description),
            event="dashboard.project_description_changed",
        )

    @app.post("/api/dashboard/projects/{name}/default")
    async def default_project_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).set_default_project(name),
            event="dashboard.default_project_changed",
        )

    @app.get("/api/dashboard/projects/{name}/import")
    async def workspace_import_status(
        name: str, request: Request, progress_only: bool = False
    ) -> Response:
        try:
            imports = repo(request).workspace_imports
            result = await asyncio.to_thread(imports.status, name, progress_only=progress_only)
            return JSONResponse(result)
        except ProjectNotFound:
            return JSONResponse({"detail": "Project not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/import/start")
    async def start_workspace_import(name: str, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _: asyncio.to_thread(repo(request).workspace_imports.start, name),
            event="workspace.import_started",
            subject=name,
        )

    @app.get("/api/dashboard/projects/{name}/import/coverage/preview")
    async def preview_project_coverage(
        name: str, request: Request, provider: str | None = None, model: str | None = None
    ) -> Response:
        try:
            value = await asyncio.to_thread(
                repo(request).workspace_imports.preview_coverage, name, provider, model
            )
            return JSONResponse(value)
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None
        except ProjectConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ProjectInvalid as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post("/api/dashboard/projects/{name}/import/stop")
    async def stop_workspace_review(name: str, request: Request) -> Response:
        return await project_change(
            request,
            WorkspaceControlInput,
            lambda body: repo(request).workspace_imports.control(
                name, body.control_id, models_of(request), resume=False
            ),
            event="workspace.review_stopped",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/import/resume")
    async def resume_workspace_review(name: str, request: Request) -> Response:
        return await project_change(
            request,
            WorkspaceControlInput,
            lambda body: repo(request).workspace_imports.control(
                name, body.control_id, models_of(request), resume=True
            ),
            event="workspace.review_resumed",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/import/coverage")
    async def start_project_coverage(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ManagedCoverageInput,
            lambda body: asyncio.to_thread(
                repo(request).workspace_imports.start_coverage,
                name,
                body.version,
                body.provider,
                body.model,
                body.links_only,
            ),
            event="workspace.coverage_requested",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/import/advance")
    async def advance_workspace_import(name: str, request: Request) -> Response:
        return await project_change(
            request,
            WorkspaceImportStep,
            lambda body: repo(request).workspace_imports.advance(
                name,
                body.version,
                github(request),
                models_of(request),
                body.provider,
                body.model,
                all_epics=body.all_epics,
                all_stories=body.all_stories,
                all_issues=body.all_issues,
                background=body.background,
            ),
            event="workspace.import_advanced",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/import/description")
    async def approve_workspace_description(name: str, request: Request) -> Response:
        return await project_change(
            request,
            WorkspaceDescription,
            lambda body: asyncio.to_thread(
                repo(request).workspace_imports.approve_description,
                name,
                body.version,
                body.description,
            ),
            event="workspace.description_approved",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/import/review")
    async def review_workspace_stage(name: str, request: Request) -> Response:
        async def apply(body: WorkspaceReviewInput) -> dict[str, Any]:
            records = repo(request)
            await asyncio.to_thread(
                records.workspace_imports.review.apply,
                name,
                body.version,
                [v.model_dump(mode="json") for v in body.choices],
            )
            return await asyncio.to_thread(records.workspace_imports.status, name)

        return await project_change(
            request,
            WorkspaceReviewInput,
            apply,
            event="workspace.stage_reviewed",
            subject=name,
            max_bytes=4_000_000,
        )

    @app.post("/api/dashboard/projects/{name}/planning/{kind}/decisions")
    async def decide_proposals(name: str, kind: Kind, request: Request) -> Response:
        return await project_change(
            request,
            ProposalDecisions,
            lambda body: asyncio.to_thread(
                repo(request).workspace_imports.review.decide,
                name,
                kind,
                body.ids,
                body.version,
                dismiss=body.dismiss,
            ),
            event="workspace.proposals_reviewed",
            subject=name,
            max_bytes=4_000_000,
        )

    @app.post("/api/dashboard/projects/{name}/planning/{kind}/{key}/proposal")
    async def edit_proposal(name: str, kind: Kind, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ProposalEdit,
            lambda body: asyncio.to_thread(
                repo(request).workspace_imports.review.edit,
                name,
                kind,
                key,
                body.version,
                body.title,
                body.description,
                body.parents,
                body.execution_type,
            ),
            event="workspace.proposal_edited",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/planning/issues/{key}/approve")
    async def approve_workspace_issue(name: str, key: UUID, request: Request) -> Response:
        async def approve(_: PlainChangeInput) -> dict[str, Any]:
            publishing = repo(request).publishing
            action = await asyncio.to_thread(publishing.approve, name, key)
            if action:
                return await publishing.sync(name, action, github(request))
            return {"project": name, "status": "done"}

        return await project_change(
            request, PlainChangeInput, approve, event="workspace.issue_approved", subject=name
        )

    @app.get("/api/dashboard/projects/{name}/synchronization")
    async def outstanding_workspace_actions(name: str, request: Request) -> Response:
        try:
            items = await asyncio.to_thread(repo(request).publishing.outstanding, name)
            return JSONResponse({"items": items})
        except ProjectNotFound:
            return JSONResponse({"detail": "Project not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/synchronization/{key}")
    async def reconcile_workspace_action(name: str, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _: repo(request).publishing.sync(name, key, github(request)),
            event="workspace.github_reconciled",
            subject=name,
        )

    @app.get("/api/dashboard/projects/{name}/connections")
    async def connections_list(name: str, request: Request) -> Response:
        try:
            records = repo(request)
            result = await asyncio.to_thread(records.connections.listing, name)
            result["models"] = await asyncio.to_thread(records.workspace_imports.model_choices)
            return JSONResponse(result)
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.get("/api/dashboard/projects/{name}/connections/{key}")
    async def connection_details(
        name: str,
        key: UUID,
        request: Request,
        page: int = Query(1, ge=1),
        status: Literal["", "pending", "approved", "dismissed", "outdated"] = "",
    ) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(repo(request).connections.detail, name, key, page, status)
            )
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Connection not found") from None

    @app.get("/api/dashboard/projects/{name}/connections/{key}/options")
    async def connection_options(
        name: str,
        key: UUID,
        request: Request,
        kind: Kind,
        side: Literal["source", "target"],
        search: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
    ) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    repo(request).connections.options, name, key, side, kind, search, page
                )
            )
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Connection not found") from None

    @app.post("/api/dashboard/projects/{name}/connections")
    @app.post("/api/dashboard/projects/{name}/connections/{key}/update")
    async def save_connection(name: str, request: Request, key: UUID | None = None) -> Response:
        return await project_change(
            request,
            ConnectionInput,
            lambda b: asyncio.to_thread(
                repo(request).connections.save, name, b.other, b.relation, b.purpose, key
            ),
            event="connection.saved",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/connections/{key}/disconnect")
    async def disconnect_connection(name: str, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _: asyncio.to_thread(repo(request).connections.disconnect, name, key),
            event="connection.disconnected",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/connections/{key}/pause")
    async def pause_connection_analysis(name: str, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            RecordPauseInput,
            lambda body: asyncio.to_thread(
                repo(request).connections.pause_analysis, name, key, body.control_id, body.paused
            ),
            event="connection.analysis_pause_changed",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/connections/{key}/analyze")
    async def analyze_connection(name: str, key: UUID, request: Request) -> Response:
        async def start(body: ConnectionAnalysisInput) -> dict[str, str]:
            records = repo(request)
            if body.provider or body.model:
                choices = await asyncio.to_thread(records.workspace_imports.model_choices)
                if {"provider": body.provider, "model": body.model} not in choices:
                    raise ProjectInvalid("Choose an available model from Accounts")
            return await asyncio.to_thread(
                records.connections.analyze, name, key, body.provider, body.model
            )

        return await project_change(
            request, ConnectionAnalysisInput, start, event="connection.queued", subject=name
        )

    @app.post("/api/dashboard/projects/{name}/connections/{key}/links")
    async def add_connection_link(name: str, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ConnectionLinkInput,
            lambda b: asyncio.to_thread(
                repo(request).connections.manual,
                name,
                key,
                b.source_id,
                b.target_kind,
                b.target_id,
                b.reason,
            ),
            event="connection.linked",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/connections/{key}/links/approve-all")
    async def connect_all_issues(name: str, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ConnectionBulkLinksInput,
            lambda b: asyncio.to_thread(
                repo(request).connections.connect_all_issues, name, key, b.through
            ),
            event="connection.issue_links_reviewed",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/connections/{key}/findings/{finding}")
    async def decide_connection(name: str, key: UUID, finding: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ConnectionDecisionInput,
            lambda b: asyncio.to_thread(
                repo(request).connections.decide, name, key, finding, b.version, b.accept
            ),
            event="connection.reviewed",
            subject=name,
        )

    @app.get("/api/dashboard/projects/{name}/statistics")
    async def workspace_statistics(name: str, request: Request, days: int = 7) -> Response:
        try:
            data = await asyncio.to_thread(repo(request).workspace_metrics.daily, name, days)
            return JSONResponse(data)
        except ProjectNotFound:
            return JSONResponse({"detail": "Project not found"}, status_code=404)
        except ProjectInvalid as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    @app.get("/api/dashboard/projects/{name}/planning-reviews")
    async def planning_reviews(name: str, request: Request, page: int = Query(1, ge=1)) -> Response:
        records = repo(request)
        try:
            result = await asyncio.to_thread(records.planning_analysis.status, name, page)
            result["models"] = await asyncio.to_thread(records.workspace_imports.model_choices)
            return JSONResponse(result)
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.post("/api/dashboard/projects/{name}/review-budget")
    async def authorize_review_budget(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ReviewBudgetInput,
            lambda body: asyncio.to_thread(
                review_budget.authorize,
                repo(request).sessions,
                name,
                body.expected_limit,
                body.token_limit,
            ),
            event="project.review_budget_authorized",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/planning-reviews/pause")
    async def pause_planning_queue(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ReviewPauseInput,
            lambda body: asyncio.to_thread(
                repo(request).planning_analysis.pause_queue, name, body.paused
            ),
            event="planning.review_queue_pause_changed",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/planning-reviews/{kind}/{key}/pause")
    async def pause_planning_record(name: str, kind: Kind, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            RecordPauseInput,
            lambda body: asyncio.to_thread(
                repo(request).planning_analysis.control_record,
                name,
                kind,
                key,
                body.control_id,
                body.paused,
            ),
            event="planning.review_pause_changed",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/planning-reviews/model")
    async def planning_review_model(name: str, request: Request) -> Response:
        async def change(body: ConnectionAnalysisInput) -> dict[str, str]:
            records = repo(request)
            choices = await asyncio.to_thread(records.workspace_imports.model_choices)
            if (
                not body.provider
                or not body.model
                or {"provider": body.provider, "model": body.model} not in choices
            ):
                raise ProjectInvalid("Choose an available model from Accounts")
            return await asyncio.to_thread(
                records.planning_analysis.set_model, name, body.provider, body.model
            )

        return await project_change(
            request,
            ConnectionAnalysisInput,
            change,
            event="planning.review_model_selected",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/planning-reviews/{kind}/{key}/retry")
    async def retry_planning_review(name: str, kind: Kind, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _: asyncio.to_thread(repo(request).planning_analysis.retry, name, kind, key),
            event="planning.review_retried",
            subject=name,
        )

    @app.get("/api/dashboard/projects/{name}/planning")
    async def project_planning(name: str, request: Request) -> Response:
        try:
            return JSONResponse(await asyncio.to_thread(repo(request).planning.listing, name))
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.get("/api/dashboard/projects/{name}/planning/counts")
    async def planning_counts(name: str, request: Request) -> Response:
        try:
            return JSONResponse(await asyncio.to_thread(repo(request).planning.counts, name))
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.get("/api/dashboard/projects/{name}/planning/{kind}")
    async def planning_page(
        name: str,
        kind: Kind,
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
        search: str = Query(default="", max_length=200, pattern=r"^[^\x00]*$"),
        epic: UUID | None = None,
        record: UUID | None = None,
        connected: str = Query("", max_length=32),
        issue_state: IssueView | None = None,
        state: PlanningState | None = None,
    ) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    repo(request).planning.page,
                    name,
                    kind,
                    page,
                    page_size,
                    search,
                    epic,
                    record,
                    connected,
                    issue_state,
                    state,
                )
            )
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.get("/api/dashboard/projects/{name}/planning/{kind}/pending")
    async def pending_planning(
        name: str,
        kind: Kind,
        request: Request,
        issue_state: IssueView | None = None,
        state: PlanningState | None = None,
    ) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    repo(request).planning.pending, name, kind, issue_state, state
                )
            )
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.get("/api/dashboard/projects/{name}/planning/{kind}/parents")
    async def planning_parents(name: str, kind: Kind, request: Request) -> Response:
        try:
            return JSONResponse(await asyncio.to_thread(repo(request).planning.parents, name, kind))
        except ProjectNotFound:
            raise HTTPException(status_code=404, detail="Project not found") from None

    @app.post("/api/dashboard/projects/{name}/planning/issues/{key}/stories")
    async def link_issue_stories(name: str, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            WorkspaceIssueStories,
            lambda body: asyncio.to_thread(
                repo(request).planning.link_stories, name, key, body.story_ids, body.execution_type
            ),
            event="workspace.issue_stories_linked",
            subject=name,
        )

    async def save_planning(name: str, kind: Kind, key: UUID | None, request: Request) -> Response:
        schema = {"epics": EpicInput, "stories": StoryInput, "issues": WorkspaceIssueInput}[kind]

        async def change(body: EpicInput) -> dict[str, str]:
            parents = (
                body.epic_ids
                if isinstance(body, StoryInput)
                else (body.story_ids if isinstance(body, WorkspaceIssueInput) else [])
            )
            return await asyncio.to_thread(
                repo(request).planning.save,
                name,
                kind,
                body.title,
                body.description,
                parents,
                key,
                body.execution_type if isinstance(body, WorkspaceIssueInput) else None,
            )

        return await project_change(
            request,
            schema,
            change,
            max_bytes=256_000,
            event="workspace.planning_saved",
            subject=name,
            status_code=201 if key is None else 200,
        )

    @app.post("/api/dashboard/projects/{name}/planning/{kind}")
    async def create_planning(name: str, kind: Kind, request: Request) -> Response:
        return await save_planning(name, kind, None, request)

    @app.post("/api/dashboard/projects/{name}/planning/{kind}/{key}/update")
    async def update_planning(name: str, kind: Kind, key: UUID, request: Request) -> Response:
        return await save_planning(name, kind, key, request)

    @app.get("/api/dashboard/projects/{name}/planning/{kind}/{key}/impact")
    async def planning_impact(
        name: str, kind: Kind, key: UUID, operation: Literal["close", "delete"], request: Request
    ) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    repo(request).planning.lifecycle.preview, name, kind, key, operation
                )
            )
        except ProjectNotFound:
            raise HTTPException(404, "Planning record not found") from None
        except ProjectInvalid as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/api/dashboard/projects/{name}/planning/{kind}/{key}/remove")
    async def remove_planning(name: str, kind: Kind, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlanningConfirmation,
            lambda body: asyncio.to_thread(
                repo(request).planning.remove, name, kind, key, body.confirmation
            ),
            event="workspace.planning_removed",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/planning/{kind}/{key}/state")
    async def change_planning_state(name: str, kind: Kind, key: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlanningStateInput,
            lambda body: asyncio.to_thread(
                repo(request).planning.set_state, name, kind, key, body.state, body.confirmation
            ),
            event="workspace.planning_state_changed",
            subject=name,
        )

    def scope(request: Request) -> str | None:
        return request.query_params.get("scope_project")

    async def verify_scope(request: Request) -> None:
        project = scope(request)
        if project is None:
            return
        if isinstance(await require_session(request, api=True), Response):
            raise HTTPException(401, "authentication required")
        try:
            await asyncio.to_thread(repo(request).projects.identity_of, project)
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None

    async def flow_access(key: str, request: Request) -> None:
        await verify_scope(request)
        if isinstance(await require_session(request, api=True), Response):
            raise HTTPException(401, "authentication required")
        owner = await asyncio.to_thread(repo(request).flows.owner, key)
        if owner != scope(request) and not (request.method == "GET" and owner is None):
            raise HTTPException(404, "Flow not found in this library.")

    @app.get("/api/dashboard/flows", dependencies=[Depends(verify_scope)])
    async def flows_api(
        request: Request, page: int | None = None, page_size: int = 10, search: str = ""
    ) -> Response:
        """The flows a section can be assigned, and for those that run here, the version
        in force and whether a draft is being worked on."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        if page is not None:
            if page_size not in (10, 25, 50, 100) or len(search) > 200:
                raise HTTPException(422, "Invalid page size or search")
            return JSONResponse(
                await asyncio.to_thread(
                    repo(request).flows.page, scope(request), page, page_size, search.strip()
                )
            )
        return JSONResponse(
            await service(request).flows(
                scope(request), request.query_params.get("include_shared") == "true"
            )
        )

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

    @app.get("/api/dashboard/operations")
    async def shared_operations(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse({"items": await asyncio.to_thread(repo(request).sections.operations)})

    @app.get("/api/dashboard/command-profiles")
    async def command_profiles(request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            result = await asyncio.to_thread(
                repo(request).command_profiles.catalog, scope(request), repo(request).flows
            )
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None
        return JSONResponse(result)

    @app.post("/api/dashboard/operations")
    async def create_operation(request: Request) -> Response:
        return await project_change(
            request,
            SectionCreateInput,
            lambda b: asyncio.to_thread(
                repo(request).sections.save_operation, None, b.name, guide(b)
            ),
            event="dashboard.operation_created",
            entity="key",
            status_code=201,
        )

    @app.post("/api/dashboard/operations/{key}/update")
    async def edit_operation(key: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionCreateInput,
            lambda b: asyncio.to_thread(
                repo(request).sections.save_operation, key, b.name, guide(b)
            ),
            event="dashboard.operation_changed",
            subject=key,
        )

    @app.post("/api/dashboard/operations/{key}/remove")
    async def delete_operation(key: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionRemoveInput,
            lambda b: asyncio.to_thread(repo(request).sections.remove_operation, key),
            event="dashboard.operation_removed",
            subject=key,
        )

    @app.post("/api/dashboard/command-profiles")
    async def create_commands(request: Request) -> Response:
        return await project_change(
            request,
            CommandProfileInput,
            lambda b: asyncio.to_thread(
                repo(request).command_profiles.save,
                scope(request),
                None,
                b.name,
                b.install,
                b.tests,
                b.manifest,
            ),
            event="dashboard.command_profile_created",
            entity="id",
            password=True,
            status_code=201,
        )

    @app.post("/api/dashboard/command-profiles/{profile_id}/update")
    async def edit_commands(profile_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            CommandProfileInput,
            lambda b: asyncio.to_thread(
                repo(request).command_profiles.save,
                scope(request),
                profile_id,
                b.name,
                b.install,
                b.tests,
                b.manifest,
            ),
            event="dashboard.command_profile_changed",
            subject=str(profile_id),
            password=True,
        )

    @app.post("/api/dashboard/command-profiles/{profile_id}/remove")
    async def delete_commands(profile_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            CommandSelectionInput,
            lambda b: asyncio.to_thread(
                repo(request).command_profiles.remove, scope(request), profile_id
            ),
            event="dashboard.command_profile_removed",
            subject=str(profile_id),
            password=True,
        )

    @app.post("/api/dashboard/projects/{name}/command-profile")
    async def select_commands(name: str, request: Request) -> Response:
        return await project_change(
            request,
            CommandSelectionInput,
            lambda b: asyncio.to_thread(repo(request).command_profiles.choose, name, b.profile_id),
            event="dashboard.command_profile_selected",
            subject=name,
            password=True,
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

    @app.get("/api/dashboard/projects/{name}/evidence")
    async def evidence_status(name: str, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            value = await asyncio.to_thread(review_evidence.status, repo(request).sessions, name)
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None
        return JSONResponse(value)

    # Previews rank a full inventory. Queue concurrent previews in the console
    # while status, stop requests and the background worker remain independent.
    evidence_preview_lock = asyncio.Lock()

    @app.post("/api/dashboard/projects/{name}/evidence/preview")
    async def evidence_preview(name: str, request: Request) -> Response:
        async def build(body: EvidencePreviewInput) -> dict[str, Any]:
            async with evidence_preview_lock:
                return await asyncio.to_thread(
                    review_evidence.preview,
                    repo(request).sessions,
                    name,
                    body.document,
                    body.provider,
                    body.model,
                    body.page,
                    body.search,
                )

        return await project_change(
            request,
            EvidencePreviewInput,
            build,
            event="project.evidence_preview",
            subject=name,
            max_bytes=300_000,
            record=False,
        )

    @app.get("/api/dashboard/projects/{name}/evidence/reviews")
    async def evidence_reviews(
        name: str,
        request: Request,
        state: Literal["pending", "advisory", "reviewed"] = "pending",
        page: int = Query(1, ge=1, le=10_000_000),
    ) -> Response:
        auth = await require_session(request, api=True)
        if isinstance(auth, Response):
            return auth
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    evidence_verdicts.listing,
                    repo(request).sessions,
                    name,
                    state == "reviewed",
                    page,
                    state == "advisory",
                )
            )
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None

    @app.get("/api/dashboard/projects/{name}/evidence/reviews/{run_id}")
    async def evidence_review_detail(name: str, run_id: UUID, request: Request) -> Response:
        auth = await require_session(request, api=True)
        if isinstance(auth, Response):
            return auth
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    evidence_verdicts.detail, repo(request).sessions, name, run_id
                )
            )
        except ProjectNotFound:
            raise HTTPException(404, "Comparison not found") from None

    @app.post("/api/dashboard/projects/{name}/evidence/reviews/{run_id}")
    async def evidence_review_decision(name: str, run_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            EvidenceDecisionInput,
            lambda b: asyncio.to_thread(
                evidence_verdicts.decide,
                repo(request).sessions,
                name,
                run_id,
                b.scope,
                b.note,
                b.expected_revision,
            ),
            event="project.evidence_verdict_reviewed",
            subject=f"{name}/{run_id}",
        )

    @app.post("/api/dashboard/projects/{name}/evidence/runs")
    async def evidence_start(name: str, request: Request) -> Response:
        return await project_change(
            request,
            EvidenceRunInput,
            lambda b: asyncio.to_thread(
                review_evidence.start,
                repo(request).sessions,
                name,
                b.request_id,
                b.provider,
                b.model,
                b.token_cap,
                b.candidate,
            ),
            event="project.evidence_run_started",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/evidence/control")
    async def evidence_control(name: str, request: Request) -> Response:
        return await project_change(
            request,
            EvidenceControlInput,
            lambda b: asyncio.to_thread(
                review_evidence.control, repo(request).sessions, name, b.run_id, b.action
            ),
            event="project.evidence_run_controlled",
            subject=name,
        )

    @app.get("/api/dashboard/projects/{name}/review-flows")
    async def review_assignments_api(name: str, request: Request) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(review_flows.assignments, repo(request).sessions, name)
            )
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None

    @app.post("/api/dashboard/projects/{name}/review-flows/{action}")
    async def assign_review_api(name: str, action: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionFlowInput,
            lambda body: asyncio.to_thread(
                review_flows.assign, repo(request).sessions, name, action, body.flow, body.version
            ),
            event="project.review_flow_assigned",
            subject=f"{name}/{action}",
        )

    @app.get("/api/dashboard/stats")
    async def stats_api(request: Request, days: int = 30) -> Response:
        if days not in (7, 30, 90):
            raise HTTPException(422, "Choose a 7, 30 or 90 day period")
        return JSONResponse(
            await asyncio.to_thread(
                stats.report,
                repo(request).sessions,
                days,
                repo(request).workspace_metrics.exchange_rates,
            )
        )

    @app.get("/api/dashboard/stats/live")
    async def live_stats_api(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(stats.live, repo(request).sessions),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/dashboard/projects/{name}/sections/{key}/flow")
    async def assign_flow_api(name: str, key: str, request: Request) -> Response:
        return await project_change(
            request,
            SectionFlowInput,
            lambda body: service(request).assign_flow(name, key, body.flow, body.version),
            event="dashboard.section_flow_assigned",
            subject=f"{name}/{key}",
        )

    @app.get("/api/dashboard/items")
    async def items_api(
        request: Request,
        project: str | None = None,
        section: str | None = Query(default=None, max_length=40),
        state: str | None = Query(default=None, max_length=16),
        search: str | None = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1, le=10_000),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> Response:
        """The backlog: the work wanted, with the section each item is routed to."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        await check_project(request, project)
        if state is not None and state not in BACKLOG_STATES:
            raise HTTPException(status_code=422, detail="invalid state filter")
        if section is not None and section != "none" and not SECTION_KEY.fullmatch(section):
            raise HTTPException(status_code=422, detail="invalid section filter")
        return JSONResponse(
            await service(request).items(
                project=project,
                section=section,
                state=state,
                search=search,
                page=page,
                page_size=page_size,
            )
        )

    @app.get("/api/dashboard/inbox")
    async def inbox_api(request: Request) -> Response:
        """What waits for the administrator, and where to act on each thing."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).inbox())

    @app.get("/api/dashboard/projects/{name}/execution")
    async def execution_overview(name: str, request: Request) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(repo(request).execution_plans.overview, name)
            )
        except ProjectNotFound:
            return JSONResponse({"detail": "Project not found"}, status_code=404)

    @app.get("/api/dashboard/projects/{name}/execution/{epic}")
    async def preview_execution_epic(name: str, epic: UUID, request: Request) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(repo(request).execution_plans.preview, name, epic)
            )
        except ProjectNotFound:
            return JSONResponse({"detail": "Open Epic not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/plans")
    async def plan_epic(name: str, request: Request) -> Response:
        return await project_change(
            request,
            EpicQueueInput,
            lambda b: asyncio.to_thread(repo(request).execution_plans.create, name, b.epic),
            event="queue.epic_selected",
            subject=name,
        )

    @app.get("/api/dashboard/projects/{name}/plans/{key}")
    async def execution_plan(name: str, key: UUID, request: Request) -> Response:
        try:
            return JSONResponse(
                await asyncio.to_thread(repo(request).execution_plans.detail, name, key)
            )
        except ProjectNotFound:
            return JSONResponse({"detail": "Plan not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/plans/{key}/{operation}")
    async def edit_execution(name: str, key: UUID, operation: str, request: Request) -> Response:
        return await project_change(
            request,
            ExecutionPlanInput,
            lambda b: asyncio.to_thread(
                repo(request).execution_plans.change,
                name,
                key,
                b.version,
                operation,
                types=b.types,
                stories=b.stories,
                order=b.order,
                requires=b.requires,
            ),
            event="queue.plan_updated",
            subject=name,
            max_bytes=2_000_000,
        )

    @app.get("/api/dashboard/projects/{name}/queues")
    async def project_queues(name: str, request: Request) -> Response:
        return JSONResponse(await service(request).queues(project=name))

    @app.get("/api/dashboard/projects/{name}/queues/{key}")
    async def project_queue(name: str, key: UUID, request: Request) -> Response:
        try:
            result = await service(request).queue(key)
            if result["project"] != name:
                raise ProjectNotFound(str(key))
            return JSONResponse(result)
        except ProjectNotFound:
            return JSONResponse({"detail": "Queue not found in this project"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/queues/{key}/{operation}")
    async def scoped_queue_change(
        name: str, key: UUID, operation: str, request: Request
    ) -> Response:
        allowed = {
            "start": PlainChangeInput,
            "pause": PlainChangeInput,
            "discard": PlainChangeInput,
            "order": QueueItemsInput,
            "update": QueueUpdateInput,
        }
        if operation not in allowed:
            return JSONResponse({"detail": "Unknown queue action"}, status_code=404)

        async def change(body: Any) -> dict[str, object]:
            if (await service(request).queue(key))["project"] != name:
                raise ProjectNotFound(str(key))
            return await service(request).change_queue(
                key, operation, **body.model_dump(exclude={"csrf_token"})
            )

        return await project_change(
            request, allowed[operation], change, event="queue.changed", subject=name
        )

    @app.post("/api/dashboard/projects/{name}/queues/{key}/items/{item}/{operation}")
    async def scoped_queue_item(
        name: str, key: UUID, item: UUID, operation: str, request: Request
    ) -> Response:
        async def change(_: Any) -> dict[str, Any]:
            if (await service(request).queue(key))["project"] != name:
                raise ProjectNotFound(str(key))
            if operation == "retry":
                return await asyncio.to_thread(
                    repo(request).queues.requeue, key, item, "Administrator requested a retry"
                )
            if operation == "remove":
                return await service(request).change_queue(key, "remove", item=item)
            raise ProjectNotFound(operation)

        return await project_change(
            request, PlainChangeInput, change, event="queue.item_changed", subject=name
        )

    @app.get("/api/dashboard/queues")
    async def queues_api(request: Request, project: str | None = None) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).queues(project=project))

    @app.get("/api/dashboard/queues/{queue_id}")
    async def queue_api(queue_id: UUID, request: Request) -> Response:
        """One queue with its items in order."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).queue(queue_id))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/queues")
    async def create_queue_api(name: str, request: Request) -> Response:
        """Make a queue from items of the project. It is a draft until it is started."""
        return await project_change(
            request,
            QueueCreateInput,
            lambda body: service(request).create_queue(
                name, name=body.name, items=body.items, at_once=body.at_once
            ),
            event="dashboard.queue_created",
            status_code=201,
            entity="queue",
        )

    def queue_change(change: str, model: type[Any], event: str) -> Any:
        async def handler(queue_id: UUID, request: Request) -> Response:
            return await project_change(
                request,
                model,
                lambda body: service(request).change_queue(
                    queue_id,
                    change,
                    **{
                        key: getattr(body, key)
                        for key in ("items", "name", "at_once")
                        if hasattr(body, key)
                    },
                ),
                event=event,
                entity="queue",
            )

        return handler

    for change, model, event in (
        ("add", QueueItemsInput, "dashboard.queue_extended"),
        ("order", QueueItemsInput, "dashboard.queue_reordered"),
        ("update", QueueUpdateInput, "dashboard.queue_changed"),
        ("start", PlainChangeInput, "dashboard.queue_started"),
        ("pause", PlainChangeInput, "dashboard.queue_paused"),
        ("discard", PlainChangeInput, "dashboard.queue_discarded"),
    ):
        app.post(f"/api/dashboard/queues/{{queue_id}}/{change}")(queue_change(change, model, event))

    @app.post("/api/dashboard/queues/{queue_id}/items/{item_id}/remove")
    async def remove_queue_item_api(queue_id: UUID, item_id: UUID, request: Request) -> Response:
        """Take an item that still waits out of a queue. It stays in the backlog."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).change_queue(queue_id, "remove", item=item_id),
            event="dashboard.queue_shortened",
            entity="queue",
        )

    @app.get("/api/dashboard/budgets")
    async def budgets_api(request: Request) -> Response:
        """How much may run for the platform, each project and each account, and how much
        of it is used."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).budgets())

    @app.post("/api/dashboard/budgets")
    async def set_budget_api(request: Request) -> Response:
        return await project_change(
            request,
            BudgetInput,
            lambda body: service(request).set_budget(
                body.scope, body.scope_id, at_once=body.at_once, per_day=body.per_day
            ),
            event="dashboard.budget_set",
            entity="budget",
        )

    @app.get("/api/dashboard/supervisor")
    async def supervisor_api(request: Request, after: str | None = None) -> Response:
        """The conversation with the supervisor: the latest messages, or those after a
        moment, whether it is answering, and what it proposed that waits."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        moment = None
        if after is not None:
            try:
                moment = datetime.fromisoformat(after)
            except ValueError:
                return JSONResponse({"detail": "invalid request"}, status_code=422)
        return JSONResponse(await service(request).supervisor(after=moment))

    @app.post("/api/dashboard/supervisor/messages")
    async def supervisor_say_api(request: Request) -> Response:
        return await project_change(
            request,
            SupervisorMessageInput,
            lambda body: service(request).say_to_supervisor(body.text),
            event="dashboard.supervisor_asked",
            status_code=201,
            entity="message",
        )

    @app.post("/api/dashboard/supervisor/stop")
    async def supervisor_stop_api(request: Request) -> Response:
        """Stop the turn the supervisor is answering in."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).stop_supervisor(),
            event="dashboard.supervisor_stopped",
            entity="run",
        )

    @app.get("/api/dashboard/projects/{name}/supervisor")
    async def project_supervisor(name: str, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).supervisor(thread=name))
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None

    @app.post("/api/dashboard/projects/{name}/supervisor/messages")
    async def project_supervisor_message(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ProjectSupervisorMessage,
            lambda body: service(request).say_to_supervisor(
                body.text,
                thread=name,
                projects=body.projects,
                all_reviews=body.all_reviews,
                request_id=body.request_id,
            ),
            status_code=201,
            event="dashboard.supervisor_asked",
            subject=name,
            entity="message",
        )

    @app.post("/api/dashboard/projects/{name}/supervisor/stop")
    async def project_supervisor_stop(name: str, request: Request) -> Response:
        return await project_change(
            request,
            SupervisorStopInput,
            lambda body: service(request).stop_supervisor(thread=name, run_id=body.run_id),
            event="dashboard.supervisor_stopped",
            subject=name,
            entity="run",
        )

    @app.post("/api/dashboard/projects/{name}/supervisor/actions/{action_id}/decide")
    async def project_supervisor_decide(name: str, action_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            SupervisorDecisionInput,
            lambda body: service(request).decide_action(action_id, body.accept, thread=name),
            event="dashboard.supervisor_action_decided",
            subject=name,
            entity="action",
        )

    @app.get("/api/dashboard/projects/{name}/supervisor/learning")
    async def project_process_learning(
        name: str, request: Request, page: int = Query(1, ge=1)
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(
                await asyncio.to_thread(repo(request).toolbox.learning.summary, name, page=page)
            )
        except ProjectNotFound:
            raise HTTPException(404, "Project not found") from None

    @app.get("/api/dashboard/projects/{name}/supervisor/reviews")
    async def project_supervisor_reviews(
        name: str,
        request: Request,
        page: int = 1,
        state: str = "",
        action: str = "",
        all_projects: bool = False,
        projects: str = Query("", max_length=2400),
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            result = await asyncio.to_thread(
                repo(request).toolbox.reviews.listing,
                None if all_projects else [name, *filter(None, projects.split(","))],
                page=page,
                state=state,
                action=action,
            )
            return JSONResponse(result)
        except ProjectInvalid as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/dashboard/projects/{name}/supervisor/reviews/{source}/{key}")
    async def supervisor_review_detail(
        name: str, source: str, key: UUID, request: Request, page: int = Query(1, ge=1)
    ) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(
                await asyncio.to_thread(
                    repo(request).toolbox.reviews.detail, name, source, key, page=page
                )
            )
        except ProjectNotFound:
            raise HTTPException(404, "Review not found") from None
        except ProjectInvalid as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/dashboard/supervisor/settings")
    async def supervisor_settings_api(request: Request) -> Response:
        """The supervisor's definition in force, its versions and the tools it can have."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).supervisor_settings())

    @app.get("/api/dashboard/supervisor/versions/{version}")
    async def supervisor_version_api(version: int, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).supervisor_version(version))

    @app.post("/api/dashboard/supervisor/settings")
    async def supervisor_save_api(request: Request) -> Response:
        """Save a new version of the definition. It decides what the supervisor may do
        without asking, so it asks for the password."""
        return await project_change(
            request,
            SupervisorDefinitionInput,
            lambda body: service(request).save_supervisor(
                prompt=body.prompt,
                skills=body.skills,
                tools=body.tools,
                providers=body.providers,
                account_id=body.account_id,
                exclusive=body.exclusive,
                models=body.models,
                note=body.note,
                operations=body.operations,
                efforts=body.efforts,
            ),
            event="dashboard.supervisor_defined",
            password=True,
            entity="definition",
        )

    @app.post("/api/dashboard/supervisor/model")
    async def supervisor_model_api(request: Request) -> Response:
        """Another model or reasoning level for the supervisor on one provider. It changes
        nothing of what the supervisor may do, so it does not ask for the password."""
        return await project_change(
            request,
            SupervisorModelInput,
            lambda body: service(request).choose_supervisor_model(
                body.provider, model=body.model, effort=body.effort
            ),
            event="dashboard.supervisor_model_chosen",
            entity="definition",
        )

    @app.get("/api/dashboard/supervisor/actions/{action_id}/flow")
    async def proposed_flow_api(action_id: UUID, request: Request) -> Response:
        """A flow the supervisor proposed: its document, what validation says, its prompts
        and how a run would go through it on paper."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).proposed_flow(action_id))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        except ProjectInvalid as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    @app.get("/api/dashboard/supervisor/record")
    async def supervisor_record_api(request: Request, everything: bool = False) -> Response:
        """What the supervisor did: what it applied, proposed, was refused, and what the
        administrator answered. With ``everything``, what it read as well."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).supervisor_record(everything=everything))

    @app.post("/api/dashboard/supervisor/actions/{action_id}/decide")
    async def supervisor_decide_api(action_id: UUID, request: Request) -> Response:
        """Accept or reject something the supervisor proposed and that waits."""
        return await project_change(
            request,
            SupervisorDecisionInput,
            lambda body: service(request).decide_action(action_id, body.accept),
            event="dashboard.supervisor_action_decided",
            entity="action",
        )

    @app.get("/api/dashboard/supervisor/incidents")
    async def incidents_api(request: Request, everything: bool = False) -> Response:
        """Trouble with the work that the platform noticed, and what the supervisor made
        of it: what is not closed, or with ``everything`` the closed ones as well."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).incidents(everything=everything))

    @app.post("/api/dashboard/supervisor/incidents/{incident_id}/dismiss")
    async def dismiss_incident_api(incident_id: UUID, request: Request) -> Response:
        """The administrator has seen an incident: nothing more is done about it."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).dismiss_incident(incident_id),
            event="dashboard.incident_dismissed",
            entity="incident",
        )

    @app.get("/api/dashboard/accounts")
    async def accounts_api(request: Request) -> Response:
        """The subscription accounts, the provider types, and whether a sign-in can be
        stored here at all."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        listing = await service(request).accounts()
        models = models_of(request)
        return JSONResponse(
            {
                **listing,
                "sign_in_available": cluster_of(request) is not None,
                # Whether an API key can be stored here at all.
                "keys_available": models is not None and models.sealing_key is not None,
            }
        )

    @app.post("/api/dashboard/accounts")
    async def create_account_api(request: Request) -> Response:
        return await project_change(
            request,
            AccountCreateInput,
            lambda body: service(request).create_account(
                name=body.name, provider=body.provider, endpoint=body.endpoint
            ),
            event="dashboard.account_added",
            status_code=201,
            entity="account",
        )

    @app.post("/api/dashboard/accounts/{account_id}/update")
    async def update_account_api(account_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            AccountUpdateInput,
            lambda body: service(request).update_account(
                account_id,
                name=body.name,
                max_concurrent=body.max_concurrent,
                paused=body.paused,
                endpoint=body.endpoint,
                prices=body.prices,
            ),
            event="dashboard.account_changed",
            entity="account",
        )

    @app.post("/api/dashboard/accounts/{account_id}/sign-in")
    async def sign_in_account_api(account_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            AccountSignInInput,
            lambda body: service(request).sign_in_account(
                account_id, body.credential, cluster_of(request), models_of(request)
            ),
            event="dashboard.account_signed_in",
            entity="account",
        )

    @app.post("/api/dashboard/accounts/{account_id}/check")
    async def check_account_api(account_id: UUID, request: Request) -> Response:
        """Ask the provider of an account with an API key which models the key reaches."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).check_account(account_id, models_of(request)),
            event="dashboard.account_checked",
            entity="account",
        )

    @app.post("/api/dashboard/account-models/refresh")
    async def refresh_account_models_api(request: Request) -> Response:
        async def refresh(_: PlainChangeInput) -> dict[str, int]:
            available = models_of(request)
            if available is None or available.sealing_key is None:
                raise ProjectInvalid("This installation cannot read model catalogs")
            return await available.refresh_catalogs()

        return await project_change(
            request,
            PlainChangeInput,
            refresh,
            event="dashboard.account_models_refreshed",
            subject="models",
            entity="account",
        )

    @app.post("/api/dashboard/account-prices")
    async def list_prices_api(request: Request) -> Response:
        """Read the list prices of the models the accounts' keys reach, and keep them."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).list_prices(models_of(request)),
            event="dashboard.account_prices_listed",
            subject="prices",
            entity="account",
        )

    @app.post("/api/dashboard/accounts/{account_id}/cache-trial")
    async def try_cache_api(account_id: UUID, request: Request) -> Response:
        """Ask a model of the account twice and say what was read from the provider's
        cache the second time."""
        return await project_change(
            request,
            CacheTrialInput,
            lambda body: service(request).try_cache(account_id, body.model, models_of(request)),
            event="dashboard.account_cache_tried",
            entity="account",
        )

    @app.post("/api/dashboard/accounts/{account_id}/batch-trial")
    async def try_batch_api(account_id: UUID, request: Request) -> Response:
        """Hand one small question to the account's provider as a batch."""
        return await project_change(
            request,
            CacheTrialInput,
            lambda body: service(request).try_batch(account_id, body.model, models_of(request)),
            event="dashboard.account_batch_tried",
            entity="account",
        )

    @app.post("/api/dashboard/accounts/{account_id}/try")
    async def try_account_api(account_id: UUID, request: Request) -> Response:
        """Start a run that asks the account's client one trivial question."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).try_account(account_id),
            event="dashboard.run_started",
            status_code=201,
            entity="run",
        )

    # --- triggers --------------------------------------------------------------------------

    @app.get("/api/dashboard/projects/{name}/triggers")
    async def triggers_api(name: str, request: Request) -> Response:
        """What starts the project's flows without anyone asking."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).triggers(name))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/triggers")
    async def create_trigger_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            TriggerInput,
            lambda body: service(request).create_trigger(name, body.section, body.kind, body.spec),
            event="dashboard.trigger_added",
            subject=name,
            status_code=201,
        )

    @app.post("/api/dashboard/triggers/{trigger_id}/update")
    async def update_trigger_api(trigger_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            TriggerUpdateInput,
            lambda body: service(request).update_trigger(trigger_id, body.enabled, body.spec),
            event="dashboard.trigger_changed",
        )

    @app.post("/api/dashboard/triggers/{trigger_id}/remove")
    async def remove_trigger_api(trigger_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_trigger(trigger_id),
            event="dashboard.trigger_removed",
        )

    @app.post("/api/dashboard/triggers/{trigger_id}/fire")
    async def fire_trigger_api(trigger_id: UUID, request: Request) -> Response:
        """Do now what the trigger does when its time comes."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).fire_trigger(trigger_id),
            event="dashboard.trigger_fired",
        )

    # --- flows -----------------------------------------------------------------------------

    @app.get("/api/dashboard/flows/catalogue", dependencies=[Depends(verify_scope)])
    async def flow_catalogue_api(request: Request) -> Response:
        """What a flow can be built from: tasks, prompts and budgets."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).flow_catalogue(scope(request)))

    @app.get("/api/dashboard/tasks")
    async def tasks_api(request: Request) -> Response:
        """Every task a flow can be built from, with the flows that use it."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).tasks())

    @app.post("/api/dashboard/tasks")
    async def task_create_api(request: Request) -> Response:
        """A task of the administrator's own: a kind of task with its settings, named."""
        return await project_change(
            request,
            TaskInput,
            lambda body: service(request).create_task(
                name=body.name,
                task=body.task,
                description=body.description,
                settings=body.settings,
                wording=body.wording,
            ),
            event="dashboard.task_created",
            status_code=201,
            entity="saved",
        )

    @app.post("/api/dashboard/tasks/{saved_id}")
    async def task_update_api(saved_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            TaskInput,
            lambda body: service(request).update_task(
                saved_id,
                name=body.name,
                description=body.description,
                settings=body.settings,
                wording=body.wording,
            ),
            event="dashboard.task_changed",
            entity="saved",
        )

    @app.post("/api/dashboard/tasks/{saved_id}/remove")
    async def task_remove_api(saved_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_task(saved_id),
            event="dashboard.task_removed",
            entity="saved",
        )

    @app.post("/api/dashboard/tasks/{saved_id}/apply")
    async def task_apply_api(saved_id: UUID, request: Request) -> Response:
        """Write the task as it is now into a draft of every flow that uses it."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).apply_task(saved_id),
            event="dashboard.task_applied",
            entity="saved",
        )

    # --- agents and skills -----------------------------------------------------------------

    @app.get("/api/dashboard/agents", dependencies=[Depends(verify_scope)])
    async def agents_api(request: Request) -> Response:
        """The agents and the skills the administrator defined."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).agents(scope(request)))

    def agent_fields(body: AgentInput) -> dict[str, Any]:
        return {
            "name": body.name,
            "description": body.description,
            "instructions": body.instructions,
            "settings": body.settings,
            "skills": body.skills,
        }

    @app.post("/api/dashboard/agents")
    async def agent_create_api(request: Request) -> Response:
        return await project_change(
            request,
            AgentInput,
            lambda body: service(request).save_agent(
                None, project=scope(request), **agent_fields(body)
            ),
            event="dashboard.agent_created",
            status_code=201,
            entity="agent",
        )

    @app.post("/api/dashboard/agents/{agent_id}")
    async def agent_update_api(agent_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            AgentInput,
            lambda body: service(request).save_agent(
                agent_id, project=scope(request), **agent_fields(body)
            ),
            event="dashboard.agent_changed",
            entity="agent",
        )

    @app.post("/api/dashboard/agents/{agent_id}/remove")
    async def agent_remove_api(agent_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_agent(agent_id, scope(request)),
            event="dashboard.agent_removed",
            entity="agent",
        )

    @app.post("/api/dashboard/skills")
    async def skill_create_api(request: Request) -> Response:
        return await project_change(
            request,
            SkillInput,
            lambda body: service(request).save_skill(
                None, name=body.name, description=body.description, text=body.text
            ),
            event="dashboard.skill_created",
            status_code=201,
            entity="skill",
        )

    @app.post("/api/dashboard/skills/{skill_id}")
    async def skill_update_api(skill_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            SkillInput,
            lambda body: service(request).save_skill(
                skill_id, name=body.name, description=body.description, text=body.text
            ),
            event="dashboard.skill_changed",
            entity="skill",
        )

    @app.post("/api/dashboard/skills/{skill_id}/remove")
    async def skill_remove_api(skill_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_skill(skill_id),
            event="dashboard.skill_removed",
            entity="skill",
        )

    @app.post("/api/dashboard/task-kinds/{task}/hidden")
    async def task_hidden_api(task: str, request: Request) -> Response:
        """Leave a kind of task out of the designer's list, or offer it again."""
        return await project_change(
            request,
            TaskHiddenInput,
            lambda body: service(request).hide_task(task, hidden=body.hidden),
            event="dashboard.task_kind_hidden",
            entity="saved",
        )

    @app.get("/api/dashboard/flows/{key}", dependencies=[Depends(flow_access)])
    async def flow_api(key: str, request: Request) -> Response:
        """One flow: the version in force, the draft when there is one, and its versions."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).flow(key))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.get("/api/dashboard/flows/{key}/versions/{version}", dependencies=[Depends(flow_access)])
    async def flow_version_api(key: str, version: int, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).flow_version(key, version))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.get(
        "/api/dashboard/flows/{key}/versions/{version}/export", dependencies=[Depends(flow_access)]
    )
    async def flow_export_api(key: str, version: int, request: Request) -> Response:
        """A version of a flow as a file: to keep, or to bring in elsewhere."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            shown = await service(request).flow_version(key, version)
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return Response(
            json.dumps(shown["document"], indent=2, ensure_ascii=False) + "\n",
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{key}-v{version}.flow.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/dashboard/flows")
    async def flow_create_api(request: Request) -> Response:
        """A new flow of the library, as a draft: brought in, copied, or empty."""
        return await project_change(
            request,
            FlowCreateInput,
            lambda body: service(request).create_flow(
                key=body.key,
                name=body.name,
                summary=body.summary,
                copy_of=body.copy_of,
                version=body.version,
                document=body.document,
                part=body.part,
                project=scope(request),
            ),
            event="dashboard.flow_created",
            status_code=201,
            entity="flow",
            max_bytes=FLOW_BYTES,
        )

    @app.post("/api/dashboard/flows/check")
    async def flow_check_api(request: Request) -> Response:
        """What the rules say about a flow as it is in the editor. Nothing is stored."""
        return await project_change(
            request,
            FlowCheckInput,
            lambda body: service(request).check_flow(body.document, scope(request)),
            event="dashboard.flow_checked",
            entity="flow",
            max_bytes=FLOW_BYTES,
            record=False,
        )

    @app.post("/api/dashboard/flows/saved-tasks")
    async def save_task_api(request: Request) -> Response:
        """Keep a task as it is set, under a name, for other flows."""
        return await project_change(
            request,
            SavedTaskInput,
            lambda body: service(request).save_task(
                name=body.name, task=body.task, settings=body.settings, wording=body.wording
            ),
            event="dashboard.task_saved",
            status_code=201,
            entity="saved",
        )

    @app.post("/api/dashboard/flows/saved-tasks/{saved_id}/remove")
    async def remove_saved_task_api(saved_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_saved_task(saved_id),
            event="dashboard.saved_task_removed",
            entity="saved",
        )

    @app.post("/api/dashboard/flows/{key}/draft", dependencies=[Depends(flow_access)])
    async def flow_draft_api(key: str, request: Request) -> Response:
        """Keep a changed flow as its draft. A draft runs nothing."""
        return await project_change(
            request,
            FlowDraftInput,
            lambda body: service(request).save_flow_draft(key, body.document, body.note),
            event="dashboard.flow_draft_saved",
            subject=key,
            entity="flow",
            max_bytes=FLOW_BYTES,
        )

    @app.post("/api/dashboard/flows/{key}/draft/discard", dependencies=[Depends(flow_access)])
    async def flow_draft_discard_api(key: str, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).discard_flow_draft(key),
            event="dashboard.flow_draft_discarded",
            subject=key,
            entity="flow",
        )

    @app.post("/api/dashboard/flows/{key}/remove", dependencies=[Depends(flow_access)])
    async def flow_remove_api(key: str, request: Request) -> Response:
        """Delete a flow: its versions, its draft and its evaluations. Sections that have
        it are left without one. One of the platform's own can be put back."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_flow(key),
            event="dashboard.flow_removed",
            entity="flow",
        )

    @app.post("/api/dashboard/flows/{key}/put-back", dependencies=[Depends(flow_access)])
    async def flow_put_back_api(key: str, request: Request) -> Response:
        """Have a deleted flow of the platform's own back, as the platform made it."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).put_flow_back(key),
            event="dashboard.flow_put_back",
            entity="flow",
        )

    @app.post("/api/dashboard/flows/{key}/publish", dependencies=[Depends(flow_access)])
    async def flow_publish_api(key: str, request: Request) -> Response:
        """Put the draft in force: every run that starts afterwards follows it. It asks
        for the password."""
        return await project_change(
            request,
            FlowPublishInput,
            lambda body: service(request).publish_flow(key, body.note),
            event="dashboard.flow_published",
            subject=key,
            password=True,
            entity="flow",
        )

    @app.post("/api/dashboard/flows/{key}/simulate", dependencies=[Depends(flow_access)])
    async def flow_simulate_api(key: str, request: Request) -> Response:
        """Go through a flow on paper. Nothing runs and nothing is stored."""
        return await project_change(
            request,
            FlowSimulationInput,
            lambda body: service(request).simulate_flow(
                key,
                document=body.document,
                version=body.version,
                run_id=body.run_id,
                outcomes=body.outcomes,
                scope=body.scope,
                entries=body.entries,
                max_rounds=body.max_rounds,
            ),
            event="dashboard.flow_simulated",
            subject=key,
            entity="flow",
            max_bytes=FLOW_BYTES,
            record=False,
        )

    @app.get("/api/dashboard/flows/{key}/evaluations", dependencies=[Depends(flow_access)])
    async def evaluations_api(key: str, request: Request) -> Response:
        """The evaluations of a flow, and the runs that can be gone through again: its
        own, or with ``?of=`` those of another flow of the same sort."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            of = request.query_params.get("of")
            return JSONResponse(await service(request).evaluations(key, of=of or None))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/flows/{key}/evaluations", dependencies=[Depends(flow_access)])
    async def start_evaluation_api(key: str, request: Request) -> Response:
        """Try a version of the flow on the work of runs that happened. The runs use the
        subscriptions like any other and leave nothing behind."""
        return await project_change(
            request,
            EvaluationInput,
            lambda body: service(request).start_evaluation(
                key,
                version=body.version,
                run_ids=body.run_ids,
                note=body.note,
                of=body.of,
                batch=body.batch,
            ),
            event="dashboard.evaluation_started",
            status_code=201,
            entity="evaluation",
        )

    @app.get("/api/dashboard/evaluations/{evaluation_id}")
    async def evaluation_api(evaluation_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).evaluation(evaluation_id))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/evaluations/{evaluation_id}/remove")
    async def remove_evaluation_api(evaluation_id: UUID, request: Request) -> Response:
        """Delete an evaluation that is not under way, with the runs it made."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_evaluation(evaluation_id),
            event="dashboard.evaluation_removed",
            entity="evaluation",
        )

    @app.post("/api/dashboard/batches/{batch_id}/remove")
    async def remove_batch_api(batch_id: UUID, request: Request) -> Response:
        """Delete what is kept of a batch that has ended."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_batch(batch_id),
            event="dashboard.batch_removed",
            entity="batch",
        )

    @app.post("/api/dashboard/evaluations/{evaluation_id}/stop")
    async def stop_evaluation_api(evaluation_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).stop_evaluation(evaluation_id),
            event="dashboard.evaluation_stopped",
            entity="evaluation",
        )

    @app.get("/api/dashboard/start")
    async def start_api(request: Request, days: int = 30, minutes: int = 0) -> Response:
        """The start page: the items done and the money spent on each of the last days
        (a month at most), the runs under way and the queues. ``minutes`` is how far
        ahead of UTC the console's clock is: a day is a day there."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).overview(days=days, minutes=minutes))

    @app.get("/api/dashboard/usage")
    async def usage_api(request: Request, days: int = 30) -> Response:
        """What the runs of the last days used, by project and by version of a flow."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).usage(max(1, min(days, 365))))

    @app.get("/api/dashboard/runs")
    async def runs_api(request: Request, project: str | None = None) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        await check_project(request, project)
        return JSONResponse(await service(request).runs(project=project))

    @app.get("/api/dashboard/batches")
    async def batches_api(request: Request) -> Response:
        """Questions handed to a provider as batches: the ones that are out, and the
        ones that ended last."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        return JSONResponse(await service(request).batches())

    @app.get("/api/dashboard/runs/{run_id}")
    async def run_api(run_id: UUID, request: Request) -> Response:
        """One run with its steps and what each printed."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).run(run_id))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.get("/api/dashboard/runs/{run_id}/flow")
    async def run_flow_api(run_id: UUID, request: Request) -> Response:
        """The flow the run follows, at the version it started with."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).run_flow(run_id))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/items/{item_id}/deliver")
    async def deliver_item_api(item_id: UUID, request: Request) -> Response:
        """Start a run that delivers the item's issue."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).deliver_item(item_id),
            event="dashboard.run_started",
            status_code=201,
            entity="run",
        )

    @app.post("/api/dashboard/runs/{run_id}/decide")
    async def decide_run_api(run_id: UUID, request: Request) -> Response:
        """Publish the change of a run that waits, or discard it. Nothing reaches GitHub
        without this, and it asks for the password."""
        return await project_change(
            request,
            RunDecisionInput,
            lambda body: service(request).decide_run(run_id, body.decision),
            event="dashboard.run_decided",
            password=True,
            entity="run",
        )

    @app.post("/api/dashboard/runs/{run_id}/remove")
    async def remove_run_api(run_id: UUID, request: Request) -> Response:
        """Delete a run that has ended, with its steps and their logs."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_run(run_id),
            event="dashboard.run_removed",
            entity="run",
        )

    @app.post("/api/dashboard/runs/remove-ended")
    async def remove_ended_runs_api(request: Request) -> Response:
        """Delete every run that ended at least so many days ago, of one project or of
        all. It needs the password again."""
        return await project_change(
            request,
            RunsRemoveInput,
            lambda body: service(request).remove_ended_runs(project=body.project, days=body.days),
            event="dashboard.runs_removed",
            subject="runs that have ended",
            password=True,
        )

    @app.post("/api/dashboard/items/{item_id}/remove")
    async def remove_item_api(item_id: UUID, request: Request) -> Response:
        """Delete an item of the backlog."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).remove_item(item_id),
            event="dashboard.item_removed",
            entity="item",
        )

    @app.post("/api/dashboard/runs/{run_id}/cancel")
    async def cancel_run_api(run_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).cancel_run(run_id),
            event="dashboard.run_stopped",
            entity="run",
        )

    @app.post("/api/dashboard/accounts/{account_id}/remove")
    async def remove_account_api(account_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            AccountRemoveInput,
            lambda _body: service(request).remove_account(account_id, cluster_of(request)),
            event="dashboard.account_removed",
            password=True,
            entity="account",
        )

    @app.post("/api/dashboard/projects/{name}/items/bulk")
    async def bulk_items_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ItemBulkInput,
            lambda body: service(request).bulk_items(
                name,
                section=body.section,
                state=body.state,
                search=body.search,
                expected=body.expected,
                route_to=body.route_to,
                clear_route=body.clear_route,
                state_to=body.state_to,
                remove=body.remove,
            ),
            event="dashboard.items_changed_together",
            subject=name,
            password=True,
        )

    @app.get("/api/dashboard/projects/{name}/backlog")
    async def backlog_status_api(name: str, request: Request) -> Response:
        """Whether the project's backlog can be brought in step with GitHub, and when it was."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            status = await service(request).backlog_status(name)
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse({"github": github(request) is not None, "last_sync": status})

    @app.post("/api/dashboard/projects/{name}/backlog/sync")
    async def backlog_sync_api(name: str, request: Request) -> Response:
        async def sync(_body: BacklogSyncInput) -> dict[str, object]:
            installed = github(request)
            if installed is None:
                raise ProjectConflict("The GitHub App is not configured, so issues cannot be read")
            return await service(request).sync_backlog(name, installed, full_history=True)

        return await project_change(
            request,
            BacklogSyncInput,
            sync,
            event="dashboard.backlog_synced",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/items", status_code=201)
    async def add_item_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ItemCreateInput,
            lambda body: service(request).add_item(
                name, title=body.title, section=body.section, description=body.description
            ),
            event="dashboard.item_added",
            subject=name,
            status_code=201,
        )

    @app.get("/api/dashboard/items/{item_id}")
    async def item_api(item_id: UUID, request: Request) -> Response:
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).item(item_id))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/items/{item_id}/edit")
    async def edit_item_api(item_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ItemEditInput,
            lambda body: service(request).edit_item(
                item_id, title=body.title, description=body.description
            ),
            event="dashboard.item_edited",
            subject=str(item_id),
        )

    @app.post("/api/dashboard/projects/{name}/sections/{key}/run", status_code=201)
    async def run_section_api(name: str, key: str, request: Request) -> Response:
        """Start what the section runs by itself: an audit, a search for enhancements."""
        return await project_change(
            request,
            SectionRunInput,
            lambda body: service(request).run_section(name, key, body.scope),
            event="dashboard.run_started",
            status_code=201,
            entity="run",
        )

    @app.post("/api/dashboard/items/{item_id}/route")
    async def route_item_api(item_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ItemRouteInput,
            lambda body: service(request).route_item(item_id, body.section),
            event="dashboard.item_routed",
            subject=str(item_id),
        )

    @app.post("/api/dashboard/items/{item_id}/state")
    async def item_state_api(item_id: UUID, request: Request) -> Response:
        return await project_change(
            request,
            ItemStateInput,
            lambda body: service(request).set_item_state(item_id, body.state),
            event="dashboard.item_state_changed",
            subject=str(item_id),
        )

    @app.get("/api/dashboard/projects/{name}/commands")
    async def project_commands_api(name: str, request: Request) -> Response:
        """How the project is installed and how its tests are run."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).project_commands(name))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/commands")
    async def project_commands_set_api(name: str, request: Request) -> Response:
        """The project's own commands, run from the next run on: with the password."""
        return await project_change(
            request,
            ProjectCommandsInput,
            lambda body: service(request).set_project_commands(
                name, install=body.install, tests=body.tests, platform=body.platform
            ),
            event="dashboard.project_commands_set",
            subject=name,
            password=True,
        )

    @app.get("/api/dashboard/projects/{name}/summaries")
    async def project_summaries_api(name: str, request: Request) -> Response:
        """How much of the project's code is summarised."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).project_summaries(name))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/summaries")
    async def project_summarise_api(name: str, request: Request) -> Response:
        """Start a run that asks a model what the project's files are for. The questions
        are paid by use, up to the dollars the request allows."""
        return await project_change(
            request,
            SummariesInput,
            lambda body: service(request).summarise_project(
                name,
                provider=body.provider,
                model=body.model,
                agent=body.agent,
                batches=body.batches,
                dollars=body.dollars,
                tests=body.tests,
                in_batch=body.in_batch,
            ),
            event="dashboard.project_summaries_started",
            subject=name,
            status_code=201,
        )

    @app.post("/api/dashboard/projects/{name}/summaries/forget")
    async def project_summaries_forget_api(name: str, request: Request) -> Response:
        """Drop every summary of the project's code."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).forget_project_summaries(name),
            event="dashboard.project_summaries_forgotten",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/commands/try")
    async def project_commands_try_api(name: str, request: Request) -> Response:
        """Clone, install and run the tests for the project once, without an agent."""
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).try_project_commands(name),
            event="dashboard.project_commands_tried",
            subject=name,
            status_code=201,
        )

    @app.post("/api/dashboard/projects/{name}/application")
    async def project_application_api(name: str, request: Request) -> Response:
        """Where the project's application runs, and the test identity a step that looks
        at it signs in with. The identity is stored in the cluster and never shown."""
        return await project_change(
            request,
            ProjectApplicationInput,
            lambda body: service(request).set_application(
                name,
                url=body.url,
                hosts=body.hosts,
                username=body.username,
                password=body.password,
                forget_identity=body.forget_identity,
                cluster=cluster_of(request),
            ),
            event="dashboard.project_application_set",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/pause")
    async def pause_project_api(name: str, request: Request) -> Response:
        """Start no further work on the project. What runs, runs to its end."""
        return await project_change(
            request,
            ProjectPauseInput,
            lambda body: service(request).pause_project(name, body.reason),
            event="dashboard.project_paused",
            subject=name,
        )

    @app.post("/api/dashboard/projects/{name}/resume")
    async def resume_project_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            PlainChangeInput,
            lambda _body: service(request).resume_project(name),
            event="dashboard.project_resumed",
            subject=name,
        )

    @app.get("/api/dashboard/original-records-script")
    async def records_script_api(request: Request) -> Response:
        """The script that exports a project's records on the original system's host, to
        save and run there. It only reads."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            text = export_script()
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return Response(
            text,
            media_type="text/x-python; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{EXPORT_SCRIPT}"'},
        )

    @app.get("/api/dashboard/projects/{name}/bring-over")
    async def bring_over_status_api(name: str, request: Request) -> Response:
        """Whether the project's work is on this platform, and what was brought along
        from the original system."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        try:
            return JSONResponse(await service(request).work_status(name))
        except ProjectNotFound:
            return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/api/dashboard/projects/{name}/bring-over/preview")
    async def bring_over_preview_api(name: str, request: Request) -> Response:
        """What a file of the original system's records would bring over. Nothing is
        stored."""
        return await project_change(
            request,
            RecordsInput,
            lambda body: service(request).preview_records(name, body.records),
            event="dashboard.records_previewed",
            subject=name,
            max_bytes=RECORDS_BYTES,
        )

    @app.post("/api/dashboard/projects/{name}/bring-over")
    async def bring_over_api(name: str, request: Request) -> Response:
        """Keep the original system's records of the project, make what is open into
        work here, and mark the project as cut over. It asks for the password."""
        return await project_change(
            request,
            RecordsApplyInput,
            lambda body: service(request).bring_over(
                name, body.records, even_if_running=body.even_if_running
            ),
            event="dashboard.project_cut_over",
            subject=name,
            password=True,
            max_bytes=RECORDS_BYTES,
        )

    @app.post("/api/dashboard/projects/{name}/cut-over")
    async def cut_over_api(name: str, request: Request) -> Response:
        """Mark the project's work as this platform's without bringing records over."""
        return await project_change(
            request,
            ProjectArchiveInput,
            lambda _body: service(request).cut_over(name),
            event="dashboard.project_cut_over",
            subject=name,
            password=True,
        )

    @app.post("/api/dashboard/projects/{name}/archive")
    async def archive_project_api(name: str, request: Request) -> Response:
        return await project_change(
            request,
            ProjectArchiveInput,
            lambda _body: service(request).archive_project(name),
            event="dashboard.project_removed",
            password=True,
        )

    @app.get("/api/dashboard/activity/recent")
    async def recent_activity(
        request: Request, limit: int = Query(default=500, ge=1, le=500)
    ) -> Response:
        """The record of what was done in the console: who changed what, and when."""
        authenticated = await require_session(request, api=True)
        if isinstance(authenticated, Response):
            return authenticated
        items = await repo(request).recent_events(limit=limit)
        return JSONResponse({"items": items, "last_event_id": int(items[-1]["id"]) if items else 0})

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
    @app.get("/console/{view:path}")
    async def console_page(request: Request) -> Response:
        """Open the global default workspace or serve a specific console view."""
        authenticated = await require_session(request)
        if isinstance(authenticated, Response):
            return authenticated
        view: str = request.path_params.get("view", "").removesuffix("/")
        if view in ("flow", "flows"):
            params = dict(request.query_params)
            owner = (
                await asyncio.to_thread(repo(request).flows.owner, params.get("key", ""))
                if view == "flow"
                else None
            )
            dest = "agents" if view == "flows" and params.get("tab") == "agents" else view
            if dest == "agents":
                params.pop("tab", None)
            url = request.url.replace(
                path=f"/console/workspace/{owner}/automation/{dest}"
                if owner
                else f"/console/settings/{dest}"
            ).replace_query_params(**params)
            return RedirectResponse(
                url.path + ("?" + url.query if url.query else ""), status_code=303
            )
        if view == "accounts":
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/console/settings/accounts{query}", status_code=303)
        index = CONSOLE_ROOT / "index.html"
        if not _console_view(view) or not index.is_file():
            raise HTTPException(status_code=404, detail="page not found")
        if (
            view.startswith("workspace/")
            and PROJECT_NAME.fullmatch(view.removeprefix("workspace/"))
            and request.query_params.get("import") == "1"
        ):
            return RedirectResponse(f"/console/{view}/reviews/discovery?import=1", status_code=303)
        if view in {"", "workspace"}:
            default = await service(request).default_project()
            if default is not None:
                return RedirectResponse(f"/console/workspace/{default}", status_code=303)
            if not view:
                return RedirectResponse("/console/workspace", status_code=303)
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


# A file of one project's records from the original system, as the console sends it.
RECORDS_BYTES: Final = 64 * 1024 * 1024
# A flow's document is bounded to a quarter of that by the engine; the rest is the request.
FLOW_BYTES: Final = 512 * 1024


async def _json(request: Request, limit: int = 32_768) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="unsupported JSON encoding")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="request is too large")
    body = await request.body()
    if len(body) > limit:
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


def _console_view(view: str) -> bool:
    return (
        view in CONSOLE_VIEWS
        or view in {f"settings/{v}" for v in ("flow", "flows", "operations", "agents", "commands")}
        or (
            view.startswith("workspace/")
            and PROJECT_NAME.fullmatch(
                re.sub(
                    r"/(?:settings/connections|queue|supervisor|reviews/(?:related-work|planning|discovery)"
                    r"|automation/(?:flow|flows|operations|assignments|agents|commands))$",
                    "",
                    view.removeprefix("workspace/"),
                )
            )
            is not None
        )
    )


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
