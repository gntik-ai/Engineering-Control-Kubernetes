"""The gateway between a client in a sandbox and the API of its provider.

A step whose agent is paid by an API key gets no key. Its client (Claude Code, Codex) is
pointed at this service and given a grant for that one step. For every request the
gateway checks the grant, checks that the step is still running, opens the account's
sealed key, and sends the request on to the provider with the key in place of the grant.
What the provider answers goes back as it comes, streamed, with nothing changed.

    /anthropic/<path>   ->  https://api.anthropic.com/<path>    (x-api-key)
    /openai/<path>      ->  https://api.openai.com/<path>       (Authorization: Bearer)

Nothing else is reachable through it: the address is one of the two above, never one a
request names. It has the platform's database (to read the account and the step) and the
key that opens API keys. It writes nothing, and it logs no header and no body.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from engineering_control.config import Settings, get_settings
from engineering_control.db.models import ProviderAccount, SandboxStep
from engineering_control.gateway.grants import GrantError, read
from engineering_control.logging import configure_logging
from engineering_control.models.sealing import SealError, read_sealing_key, unseal
from engineering_control.sandbox.egress import read_grant_key

LOG = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Upstream:
    url: str
    header: str
    # What stands before the key in the header.
    prefix: str = ""


UPSTREAMS: Final = {
    "anthropic": Upstream("https://api.anthropic.com", "x-api-key"),
    "openai": Upstream("https://api.openai.com", "authorization", "Bearer "),
}
# What is between this service and its two neighbours only, and never passed on.
HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
# What a client sends to say who it is: replaced by the account's key, never passed on.
CREDENTIALS: Final = frozenset({"authorization", "x-api-key", "cookie", "proxy-authorization"})
# For how long an account that was checked for a grant is not looked up again.
KNOWN_SECONDS: Final = 10.0


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(provider: str, status: int, message: str) -> JSONResponse:
    """A refusal in the words of the provider's own API, so that the client shows it."""
    if provider == "anthropic":
        kind = "authentication_error" if status == 401 else "permission_error"
        if status >= 500:
            kind = "api_error"
        body: dict[str, Any] = {"type": "error", "error": {"type": kind, "message": message}}
    else:
        body = {"error": {"message": message, "type": "invalid_request_error", "code": None}}
    return JSONResponse(body, status_code=status)


class Gateway:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        sealing_key: bytes | None,
        grant_key: bytes | None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.sessions = sessions
        self.sealing_key = sealing_key
        self.grant_key = grant_key
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=900, write=120, pool=30),
            follow_redirects=False,
            # The provider is reached directly: no proxy of the environment's.
            trust_env=False,
        )
        self._known: dict[str, tuple[float, str]] = {}

    def _key(self, provider: str, grant: str) -> str:
        """The API key a grant stands for, for that provider. Raises ``Refused``."""
        if self.grant_key is None or self.sealing_key is None:
            raise Refused(503, "The gateway has no keys to work with on this installation")
        try:
            account_id, step_id = read(self.grant_key, grant)
        except GrantError as error:
            raise Refused(401, str(error)) from error
        with self.sessions() as session:
            step = session.get(SandboxStep, step_id)
            if step is None or step.state != "running" or step.account_id != account_id:
                raise Refused(401, "The step this grant was issued for is not running")
            account = session.get(ProviderAccount, account_id)
            if (
                account is None
                or account.archived_at is not None
                or not account.sealed_key
                or account.provider != provider
            ):
                raise Refused(403, f"The grant is not for an account with a key of {provider}")
            sealed = account.sealed_key
        try:
            return unseal(self.sealing_key, account_id, sealed)
        except SealError as error:
            raise Refused(503, "The account's key cannot be opened") from error

    async def key(self, provider: str, grant: str) -> str:
        now = time.monotonic()
        known = self._known.get(f"{provider}:{grant}")
        if known is not None and now - known[0] < KNOWN_SECONDS:
            return known[1]
        found = await asyncio.to_thread(self._key, provider, grant)
        if len(self._known) > 500:
            self._known.clear()
        self._known[f"{provider}:{grant}"] = (now, found)
        return found

    async def forward(self, request: Request) -> Response:
        provider = str(request.path_params["provider"])
        upstream = UPSTREAMS.get(provider)
        if upstream is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        shown = request.headers.get("authorization") or ""
        grant = shown[7:] if shown.lower().startswith("bearer ") else ""
        grant = grant or request.headers.get("x-api-key") or ""
        try:
            key = await self.key(provider, grant.strip())
        except Refused as refused:
            LOG.warning("gateway_refused", provider=provider, status=refused.status)
            return _error(provider, refused.status, refused.message)
        headers = [
            (name, value)
            for name, value in request.headers.items()
            if name.lower() not in HOP and name.lower() not in CREDENTIALS
        ]
        headers.append((upstream.header, f"{upstream.prefix}{key}"))
        url = f"{upstream.url}/{request.path_params['path']}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        body = await request.body()
        try:
            sent = self.http.build_request(request.method, url, headers=headers, content=body)
            answer = await self.http.send(sent, stream=True)
        except httpx.HTTPError as error:
            LOG.warning("gateway_unreachable", provider=provider, error=type(error).__name__)
            return _error(provider, 502, f"The gateway could not reach {provider}")

        async def relay() -> AsyncIterator[bytes]:
            # As it comes: a stream of events is not held back, and nothing is unpacked.
            async for chunk in answer.aiter_raw():
                yield chunk

        passed = {
            name: value
            for name, value in answer.headers.items()
            if name.lower() not in HOP or name.lower() == "content-length"
        }
        LOG.info("gateway_forwarded", provider=provider, status=answer.status_code)
        return StreamingResponse(
            relay(),
            status_code=answer.status_code,
            headers=passed,
            background=BackgroundTask(answer.aclose),
        )


async def _healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def create_app(gateway: Gateway) -> Starlette:
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(
        routes=[
            Route("/healthz", _healthz),
            Route("/{provider}/{path:path}", gateway.forward, methods=methods),
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
        max_overflow=10,
        connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
    )
    gateway = Gateway(
        sessionmaker(engine, expire_on_commit=False),
        sealing_key=read_sealing_key(resolved.model_keys_key_file),
        grant_key=read_grant_key(resolved.sandbox_egress_grant_key_file),
    )
    uvicorn.run(
        create_app(gateway),
        host=resolved.gateway_host,
        port=resolved.gateway_port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    run()
