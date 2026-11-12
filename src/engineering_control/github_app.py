"""The platform's GitHub App: which repositories it may work on.

The app is identified by its id and one installation (both settings) and proves itself
with a private key (a mounted secret). From the key it signs a token valid for minutes,
exchanges it for an installation token limited to reading repository metadata and issues,
and lists the repositories of the installation and the open issues of one of them.
Nothing here writes to GitHub.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from engineering_control.config import Settings
from engineering_control.secret_files import read_mounted_secret

API = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUEST_SECONDS = 10.0
PAGE_SIZE = 100
MAX_REPOSITORIES = 2000
MAX_ISSUES = 2000
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}$")
# GitHub accepts an app token for at most ten minutes; ask for less and allow clock drift.
APP_TOKEN_SECONDS = 540
CLOCK_DRIFT_SECONDS = 60
# An installation token lives an hour; stop using one five minutes before it ends.
TOKEN_MARGIN_SECONDS = 300
LIST_CACHE_SECONDS = 60.0


class GitHubAppError(RuntimeError):
    """Something the administrator can read; never a token, a key or a response body."""


@dataclass(frozen=True)
class Repository:
    full_name: str
    private: bool
    archived: bool
    default_branch: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "private": self.private,
            "archived": self.archived,
            "default_branch": self.default_branch,
        }


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    updated_at: datetime | None


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    try:
        data = read_mounted_secret(
            path, label="GitHub App private key", minimum=256, maximum=16_384
        )
        key = serialization.load_pem_private_key(data, password=None)
    except (OSError, ValueError, TypeError) as error:
        raise GitHubAppError("The GitHub App private key could not be read") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise GitHubAppError("The GitHub App private key is not an RSA key of 2048 bits or more")
    return key


def _segment(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def app_token(app_id: int, key: rsa.RSAPrivateKey, *, now: float | None = None) -> str:
    """The short-lived token (a JWT signed RS256) with which the app speaks for itself."""
    issued = int(time.time() if now is None else now)
    header = _segment(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _segment(
        json.dumps(
            {
                "iat": issued - CLOCK_DRIFT_SECONDS,
                "exp": issued + APP_TOKEN_SECONDS,
                "iss": str(app_id),
            },
            separators=(",", ":"),
        ).encode()
    )
    signed = header + b"." + payload
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return (signed + b"." + _segment(signature)).decode("ascii")


class GitHubApp:
    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key_file: Path,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_file = private_key_file
        self.client = httpx.AsyncClient(
            base_url=API,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=REQUEST_SECONDS,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "engineering-control",
            },
        )
        self._lock = asyncio.Lock()
        self._key: rsa.RSAPrivateKey | None = None
        self._token: str | None = None
        self._token_until = 0.0
        self._repositories: list[Repository] | None = None
        self._repositories_until = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> GitHubApp | None:
        """None unless the app is named completely and its key file is configured."""
        if (
            settings.github_app_id is None
            or settings.github_app_installation_id is None
            or settings.github_app_private_key_file is None
        ):
            return None
        return cls(
            app_id=settings.github_app_id,
            installation_id=settings.github_app_installation_id,
            private_key_file=settings.github_app_private_key_file,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def repositories(self, *, refresh: bool = False) -> list[Repository]:
        """The repositories of the installation, by name. Cached for a minute."""
        async with self._lock:
            now = time.monotonic()
            if refresh or self._repositories is None or now >= self._repositories_until:
                self._repositories = await self._list()
                self._repositories_until = time.monotonic() + LIST_CACHE_SECONDS
            return list(self._repositories)

    async def repository(self, full_name: str) -> Repository | None:
        wanted = full_name.lower()
        for refresh in (False, True):
            for candidate in await self.repositories(refresh=refresh):
                if candidate.full_name.lower() == wanted:
                    return candidate
        return None

    async def open_issues(self, full_name: str) -> tuple[list[Issue], bool]:
        """The open issues of a repository, oldest first, and whether that is all of them.

        Pull requests are left out: GitHub lists them among the issues. A repository with
        issues switched off has none.
        """
        if not REPOSITORY_NAME.fullmatch(full_name) or ".." in full_name:
            raise GitHubAppError("That is not the name of a repository")
        token = await self._installation_token()
        issues: list[Issue] = []
        page = 1
        while True:
            body = await self._request(
                "GET",
                f"/repos/{full_name}/issues",
                token=token,
                params={
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": PAGE_SIZE,
                    "page": page,
                },
                not_found=f"The GitHub App cannot see the repository {full_name}",
                gone=[],
            )
            if not isinstance(body, list):
                raise GitHubAppError("GitHub gave no list of issues")
            for item in body:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                number, title = item.get("number"), item.get("title")
                if not isinstance(number, int) or not isinstance(title, str):
                    continue
                updated = item.get("updated_at")
                try:
                    updated_at = (
                        datetime.fromisoformat(updated.replace("Z", "+00:00"))
                        if isinstance(updated, str)
                        else None
                    )
                except ValueError:
                    updated_at = None
                labels = tuple(
                    label["name"]
                    for label in item.get("labels") or []
                    if isinstance(label, dict) and isinstance(label.get("name"), str)
                )
                issues.append(
                    Issue(
                        number=number,
                        title=title,
                        url=str(item.get("html_url") or ""),
                        labels=labels,
                        updated_at=updated_at,
                    )
                )
            if len(body) < PAGE_SIZE:
                return issues, True
            if len(issues) >= MAX_ISSUES:
                return issues, False
            page += 1

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        not_found: str = "GitHub does not know this installation of the app",
        gone: Any = None,
        **options: Any,
    ) -> Any:
        try:
            response = await self.client.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **options
            )
        except httpx.HTTPError as error:
            raise GitHubAppError("GitHub did not answer") from error
        if response.status_code in {401, 403}:
            raise GitHubAppError("GitHub refused the app's credentials")
        if response.status_code == 404:
            raise GitHubAppError(not_found)
        if response.status_code == 410 and gone is not None:
            return gone
        if response.status_code >= 400:
            raise GitHubAppError(f"GitHub answered {response.status_code}")
        try:
            return response.json()
        except ValueError as error:
            raise GitHubAppError("GitHub answered with something that is not JSON") from error

    async def _installation_token(self) -> str:
        if self._token is not None and time.time() < self._token_until:
            return self._token
        if self._key is None:
            self._key = await asyncio.to_thread(load_private_key, self.private_key_file)
        body = await self._request(
            "POST",
            f"/app/installations/{self.installation_id}/access_tokens",
            token=app_token(self.app_id, self._key),
            # Read only: the list of repositories and their issues, nothing else.
            json={"permissions": {"metadata": "read", "issues": "read"}},
        )
        token = body.get("token") if isinstance(body, dict) else None
        expires = body.get("expires_at") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise GitHubAppError("GitHub gave no installation token")
        try:
            until = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except ValueError as error:
            raise GitHubAppError("GitHub gave no installation token") from error
        self._token = token
        self._token_until = until - TOKEN_MARGIN_SECONDS
        return token

    async def _list(self) -> list[Repository]:
        token = await self._installation_token()
        found: dict[str, Repository] = {}
        page = 1
        while len(found) < MAX_REPOSITORIES:
            body = await self._request(
                "GET",
                "/installation/repositories",
                token=token,
                params={"per_page": PAGE_SIZE, "page": page},
            )
            items = body.get("repositories") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise GitHubAppError("GitHub gave no list of repositories")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
                    continue
                found[item["full_name"].lower()] = Repository(
                    full_name=item["full_name"],
                    private=bool(item.get("private")),
                    archived=bool(item.get("archived")),
                    default_branch=str(item.get("default_branch") or "main"),
                )
            if len(items) < PAGE_SIZE:
                break
            page += 1
        return sorted(found.values(), key=lambda repository: repository.full_name.lower())
