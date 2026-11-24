"""The platform's GitHub App: which repositories it may work on, and how a change reaches one.

The app is identified by its id and one installation (both settings) and proves itself
with a private key (a mounted secret). From the key it signs a token valid for minutes and
exchanges it for an installation token.

For reading (the repositories of the installation, the open issues of one, one issue) the
token is limited to repository metadata and issues. For a run it issues a token for one
repository and one purpose: reading its contents to check it out, or, when the
administrator has approved a change, writing one new branch and opening a pull request.
Such a token is revoked when the purpose is served. Nothing is ever merged from here, and
no existing branch is moved.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

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
MAX_ISSUE_BODY = 60_000
COMMIT = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$")
# A regular file, an executable, a symbolic link. Never a submodule or a directory.
FILE_MODES = frozenset({"100644", "100755", "120000"})


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


@dataclass(frozen=True)
class IssueText:
    """One issue as the agents are to read it."""

    number: int
    title: str
    body: str
    url: str
    state: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ChangedFile:
    """One file of a change. ``content`` is None for a file the change deletes."""

    path: str
    mode: str
    content: bytes | None


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    # The commit the pull request is for: what GitHub's checks run on, and what is merged.
    sha: str = ""


# How GitHub says a check ended, and which of those ends count against a change.
CHECK_FAILED: Final = (
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
)
MERGE_METHODS: Final = ("squash", "merge", "rebase")


@dataclass(frozen=True)
class Checks:
    """What GitHub's checks say of one commit: its workflows' jobs and the statuses
    other services report. ``state`` is "none" (nothing was run), "pending", "failed"
    (at least one ended badly, whatever the others do) or "passed"."""

    state: str
    total: int
    done: int
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()


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

    # --- one repository: its issue, a token for it, a change published to it -------------

    async def issue(self, full_name: str, number: int) -> IssueText:
        if not REPOSITORY_NAME.fullmatch(full_name) or number < 1:
            raise GitHubAppError("That is not an issue of a repository")
        body = await self._request(
            "GET",
            f"/repos/{full_name}/issues/{number}",
            token=await self._installation_token(),
            not_found="GitHub does not know this issue",
        )
        if not isinstance(body, dict) or "pull_request" in body:
            raise GitHubAppError("GitHub does not know this issue")
        return IssueText(
            number=number,
            title=str(body.get("title") or "")[:300],
            body=str(body.get("body") or "")[:MAX_ISSUE_BODY],
            url=str(body.get("html_url") or ""),
            state=str(body.get("state") or ""),
            labels=tuple(
                str(label.get("name"))
                for label in body.get("labels") or []
                if isinstance(label, dict) and label.get("name")
            ),
        )

    async def repository_token(self, full_name: str, permissions: dict[str, str]) -> str:
        """A token for one repository and the named permissions only, valid for an hour.
        It is not kept: the caller uses it and revokes it."""
        if not REPOSITORY_NAME.fullmatch(full_name):
            raise GitHubAppError("That is not a repository")
        if self._key is None:
            self._key = await asyncio.to_thread(load_private_key, self.private_key_file)
        try:
            body = await self._request(
                "POST",
                f"/app/installations/{self.installation_id}/access_tokens",
                token=app_token(self.app_id, self._key),
                json={"repositories": [full_name.split("/", 1)[1]], "permissions": permissions},
            )
        except GitHubAppError as error:
            if "422" in str(error):
                wanted = ", ".join(f"{name}: {level}" for name, level in permissions.items())
                raise GitHubAppError(
                    f"The GitHub App is not allowed what this needs on {full_name} ({wanted}). "
                    "Grant it in the app's settings and accept the change for the organisation."
                ) from error
            raise
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAppError("GitHub gave no token for the repository")
        return token

    async def revoke(self, token: str) -> None:
        """End a token before its hour is over. A token that is already gone is fine."""
        with contextlib.suppress(GitHubAppError):
            await self._request("DELETE", "/installation/token", token=token, empty=True)

    async def branch_head(self, full_name: str, branch: str, *, token: str) -> str:
        body = await self._request(
            "GET",
            f"/repos/{full_name}/git/ref/heads/{branch}",
            token=token,
            not_found=f"{full_name} has no branch {branch}",
        )
        sha = (body.get("object") or {}).get("sha") if isinstance(body, dict) else None
        if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
            raise GitHubAppError(f"{full_name} has no branch {branch}")
        return sha

    async def workspace_head(
        self, repository: str, branch: str, *, token: str
    ) -> tuple[str, str] | None:
        try:
            commit = await self.branch_head(repository, branch, token=token)
        except GitHubAppError as error:
            if "409" not in str(error):
                raise
            metadata = await self._request("GET", f"/repos/{repository}", token=token)
            if isinstance(metadata, dict) and metadata.get("size") == 0:
                return None
            raise
        data = await self._request("GET", f"/repos/{repository}/git/commits/{commit}", token=token)
        tree = (data.get("tree") or {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(tree, str) or not COMMIT.fullmatch(tree):
            raise GitHubAppError("GitHub returned no root tree for the repository")
        return commit, tree

    async def commit_tree(
        self, repository: str, commit: str, *, token: str
    ) -> tuple[str, str | None]:
        """The root tree of that commit, and when it was committed."""
        if not COMMIT.fullmatch(commit):
            raise GitHubAppError("Invalid commit")
        data = await self._request(
            "GET",
            f"/repos/{repository}/git/commits/{commit}",
            token=token,
            not_found=f"{repository} has no commit {commit[:12]}",
        )
        tree = (data.get("tree") or {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(tree, str) or not COMMIT.fullmatch(tree):
            raise GitHubAppError("GitHub returned no root tree for the commit")
        when = (data.get("committer") or {}).get("date")
        return tree, when if isinstance(when, str) else None

    async def workspace_tree(
        self, repository: str, tree: str, *, token: str, recursive: bool
    ) -> tuple[list[dict[str, Any]], bool]:
        if not COMMIT.fullmatch(tree):
            raise GitHubAppError("Invalid repository tree")
        data = await self._request(
            "GET",
            f"/repos/{repository}/git/trees/{tree}",
            token=token,
            params={"recursive": "1"} if recursive else {},
        )
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            raise GitHubAppError("GitHub returned an incomplete repository tree")
        return data["tree"], bool(data.get("truncated"))

    async def workspace_blob(self, repository: str, sha: str, *, token: str) -> str:
        if not COMMIT.fullmatch(sha):
            raise GitHubAppError("Invalid repository file")
        data = await self._request("GET", f"/repos/{repository}/git/blobs/{sha}", token=token)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise GitHubAppError("GitHub returned an unreadable file")
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubAppError("GitHub returned an unreadable file") from error

    async def workspace_history(
        self, repository: str, page: int, *, since: str | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        """Page through creation order, including closed issues and excluding pull requests."""
        if not REPOSITORY_NAME.fullmatch(repository) or page < 1:
            raise GitHubAppError("Invalid issue history request")
        raw = await self._request(
            "GET",
            f"/repos/{repository}/issues",
            token=await self._installation_token(),
            params={
                "state": "all",
                "sort": "created",
                "direction": "asc",
                **({"since": since} if since else {}),
                "per_page": PAGE_SIZE,
                "page": page,
            },
        )
        if not isinstance(raw, list):
            raise GitHubAppError("GitHub returned no issue history")
        rows = []
        for issue in raw:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            if (
                not isinstance(issue.get("number"), int)
                or issue.get("state") not in ("open", "closed")
                or not isinstance(issue.get("title"), str)
            ):
                raise GitHubAppError("GitHub returned an incomplete issue")
            rows.append(
                {
                    key: issue.get(key)
                    for key in (
                        "number",
                        "title",
                        "body",
                        "state",
                        "state_reason",
                        "created_at",
                        "updated_at",
                        "closed_at",
                        "html_url",
                    )
                }
                | {
                    "actor": (issue.get("user") or {}).get("login"),
                    "issue_type": (issue.get("type") or {}).get("name"),
                    "labels": [
                        label["name"]
                        for label in issue.get("labels", [])
                        if isinstance(label, dict) and isinstance(label.get("name"), str)
                    ],
                }
            )
        return rows, len(raw) < PAGE_SIZE

    async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
        if not REPOSITORY_NAME.fullmatch(repository) or number < 1:
            raise GitHubAppError("Invalid issue reference")
        data = await self._request(
            "GET", f"/repos/{repository}/issues/{number}", token=await self._installation_token()
        )
        if (
            not isinstance(data, dict)
            or "pull_request" in data
            or data.get("number") != number
            or data.get("state") not in ("open", "closed")
        ):
            raise GitHubAppError("GitHub returned a different record for this issue")
        return data

    async def classify_workspace_issue(
        self, repository: str, number: int, category: str, *, token: str
    ) -> list[str]:
        """Change only the two mutually exclusive labels on a verified Issue."""
        if category not in ("bug", "enhancement"):
            raise GitHubAppError("Invalid Issue classification")
        await self.workspace_issue(repository, number)
        root = f"/repos/{repository}"
        path = f"{root}/issues/{number}/labels"
        opposite = "enhancement" if category == "bug" else "bug"

        def names(data: Any) -> list[str]:
            if not isinstance(data, list):
                raise GitHubAppError("GitHub did not return Issue labels")
            return [
                row["name"]
                for row in data
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            ]

        try:
            labels = names(
                await self._request("POST", path, token=token, json={"labels": [category]})
            )
        except GitHubAppError as error:
            if str(error) != "GitHub answered 422":
                raise
            labels = []
        if category not in {name.casefold() for name in labels}:
            # Repositories are allowed to have removed their default labels.
            try:
                await self._request(
                    "POST",
                    root + "/labels",
                    token=token,
                    json={"name": category, "color": "d73a4a" if category == "bug" else "a2eeef"},
                )
            except GitHubAppError:
                await self._request("GET", f"{root}/labels/{category}", token=token)
            labels = names(
                await self._request("POST", path, token=token, json={"labels": [category]})
            )
        if opposite in {name.casefold() for name in labels}:
            labels = names(await self._request("DELETE", f"{path}/{opposite}", token=token))
        if category not in {name.casefold() for name in labels} or opposite in {
            name.casefold() for name in labels
        }:
            raise GitHubAppError("GitHub has not confirmed the Issue classification")
        return labels

    async def comment_workspace_issue(
        self, repository: str, number: int, body: str, marker: str, *, token: str
    ) -> bool:
        """Leave a comment on the issue, once: a comment that carries the marker is not
        written again. Returns whether it was written now."""
        if not REPOSITORY_NAME.fullmatch(repository) or number < 1 or not body.strip():
            raise GitHubAppError("Invalid issue comment")
        path = f"/repos/{repository}/issues/{number}/comments"
        page = 1
        while page <= 10:
            listed = await self._request(
                "GET", path, token=token, params={"per_page": 100, "page": page}
            )
            if not isinstance(listed, list):
                raise GitHubAppError("GitHub did not list the issue's comments")
            if any(marker in str(entry.get("body") or "") for entry in listed):
                return False
            if len(listed) < 100:
                break
            page += 1
        made = await self._request(
            "POST", path, token=token, json={"body": body.rstrip() + "\n\n" + marker}
        )
        if not isinstance(made, dict) or not made.get("id"):
            raise GitHubAppError("GitHub did not confirm the comment")
        return True

    async def close_workspace_issue(
        self, repository: str, number: int, *, token: str, reason: str | None = None
    ) -> None:
        """Close the issue; ``reason`` tells GitHub why: ``not_planned`` or ``completed``."""
        if not REPOSITORY_NAME.fullmatch(repository) or number < 1:
            raise GitHubAppError("Invalid issue reference")
        if reason not in (None, "not_planned", "completed"):
            raise GitHubAppError("Invalid reason for closing an issue")
        change = {"state": "closed", **({"state_reason": reason} if reason else {})}
        data = await self._request(
            "PATCH", f"/repos/{repository}/issues/{number}", token=token, json=change
        )
        if (
            not isinstance(data, dict)
            or data.get("state") != "closed"
            or data.get("number") != number
        ):
            raise GitHubAppError("GitHub did not confirm the issue closure")

    async def create_workspace_issue(
        self, repository: str, title: str, body: str, *, token: str
    ) -> int:
        if not REPOSITORY_NAME.fullmatch(repository):
            raise GitHubAppError("Invalid issue repository")
        data = await self._request(
            "POST", f"/repos/{repository}/issues", token=token, json={"title": title, "body": body}
        )
        if not isinstance(data, dict) or not isinstance(data.get("number"), int):
            raise GitHubAppError("GitHub did not return the new issue number")
        return int(data["number"])

    async def workspace_published(self, repository: str, marker: str) -> int | None:
        """Find this app's marker in open and closed issues of this repository."""
        if self._key is None:
            self._key = await asyncio.to_thread(load_private_key, self.private_key_file)
        app = await self._request("GET", "/app", token=app_token(self.app_id, self._key))
        if not isinstance(app, dict) or app.get("id") != self.app_id or not app.get("slug"):
            raise GitHubAppError("GitHub did not confirm the publishing app identity")
        actor = str(app["slug"]) + "[bot]"
        page = 1
        found = None
        while True:
            issues, end = await self.workspace_history(repository, page)
            for issue in issues:
                if issue["actor"] == actor and marker in str(issue.get("body") or "").splitlines():
                    if found is not None:
                        raise GitHubAppError("More than one issue carries this approval reference")
                    found = int(issue["number"])
            if end:
                return found
            page += 1

    async def publish(
        self,
        full_name: str,
        *,
        base_branch: str,
        base_sha: str,
        branch: str,
        message: str,
        title: str,
        body: str,
        files: list[ChangedFile],
    ) -> PullRequest:
        """Put a change on a new branch as one commit on top of ``base_sha`` and open a
        pull request for it. Nothing is merged, and no existing branch is moved.

        The commit is made through GitHub's API from the files' contents, so nothing a
        sandbox produced is ever executed or pushed from here.
        """
        if not files or not COMMIT.fullmatch(base_sha) or not BRANCH.fullmatch(branch):
            raise GitHubAppError("There is no change to publish")
        token = await self.repository_token(
            full_name, {"contents": "write", "pull_requests": "write"}
        )
        repository = f"/repos/{full_name}"
        missing = f"{full_name} no longer has what the change was made on"
        try:
            base = await self._request(
                "GET", f"{repository}/git/commits/{base_sha}", token=token, not_found=missing
            )
            entries: list[dict[str, Any]] = []
            for file in files:
                if file.mode not in FILE_MODES:
                    raise GitHubAppError(f"{file.path} is not a file a change can hold")
                sha: str | None = None
                if file.content is not None:
                    blob = await self._request(
                        "POST",
                        f"{repository}/git/blobs",
                        token=token,
                        not_found=missing,
                        json={
                            "content": base64.b64encode(file.content).decode("ascii"),
                            "encoding": "base64",
                        },
                    )
                    sha = blob["sha"]
                entries.append({"path": file.path, "mode": file.mode, "type": "blob", "sha": sha})
            tree = await self._request(
                "POST",
                f"{repository}/git/trees",
                token=token,
                not_found=missing,
                json={"base_tree": base["tree"]["sha"], "tree": entries},
            )
            commit = await self._request(
                "POST",
                f"{repository}/git/commits",
                token=token,
                not_found=missing,
                json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
            )
            await self._request(
                "POST",
                f"{repository}/git/refs",
                token=token,
                not_found=missing,
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
            pull = await self._request(
                "POST",
                f"{repository}/pulls",
                token=token,
                not_found=missing,
                json={"title": title[:250], "head": branch, "base": base_branch, "body": body},
            )
        except (KeyError, TypeError) as error:
            raise GitHubAppError("GitHub answered with something unexpected") from error
        finally:
            await self.revoke(token)
        number, url = pull.get("number"), pull.get("html_url")
        if not isinstance(number, int) or not isinstance(url, str):
            raise GitHubAppError("GitHub did not say where the pull request is")
        return PullRequest(number=number, url=url, sha=str(commit.get("sha") or ""))

    async def checks(self, full_name: str, sha: str) -> Checks:
        """What GitHub's checks say of a commit, read with a token that may read checks
        and statuses of that repository and nothing else."""
        if not COMMIT.fullmatch(sha):
            raise GitHubAppError("That is not a commit")
        token = await self.repository_token(full_name, {"checks": "read", "statuses": "read"})
        missing = f"{full_name} does not have the commit {sha[:12]}"
        try:
            runs = await self._request(
                "GET",
                f"/repos/{full_name}/commits/{sha}/check-runs",
                token=token,
                not_found=missing,
                params={"per_page": 100},
            )
            status = await self._request(
                "GET", f"/repos/{full_name}/commits/{sha}/status", token=token, not_found=missing
            )
        finally:
            await self.revoke(token)
        failed: list[str] = []
        pending: list[str] = []
        total = 0
        listed = runs.get("check_runs") if isinstance(runs, dict) else None
        for run in listed if isinstance(listed, list) else []:
            if not isinstance(run, dict):
                continue
            total += 1
            name = " ".join(str(run.get("name") or "a check").split())[:80]
            if run.get("status") != "completed":
                pending.append(name)
            elif run.get("conclusion") in CHECK_FAILED:
                failed.append(name)
        reported = status.get("statuses") if isinstance(status, dict) else None
        for entry in reported if isinstance(reported, list) else []:
            if not isinstance(entry, dict):
                continue
            total += 1
            name = " ".join(str(entry.get("context") or "a status").split())[:80]
            if entry.get("state") == "pending":
                pending.append(name)
            elif entry.get("state") in ("failure", "error"):
                failed.append(name)
        more = runs.get("total_count") if isinstance(runs, dict) else None
        if isinstance(more, int) and more > total:
            # More checks than one page holds: the ones not seen are not known to be done.
            pending.append(f"{more - total} more")
            total = more
        state = "none" if total == 0 else "failed" if failed else "pending" if pending else "passed"
        return Checks(
            state=state,
            total=total,
            done=total - len(pending),
            failed=tuple(failed[:20]),
            pending=tuple(pending[:20]),
        )

    async def merge(self, full_name: str, number: int, *, sha: str, method: str) -> str:
        """Merge a pull request as it stands at ``sha``, and at no other commit: GitHub
        refuses when the branch has moved since. Returns the commit the merge made."""
        if method not in MERGE_METHODS or not COMMIT.fullmatch(sha) or number < 1:
            raise GitHubAppError("That is not a pull request that can be merged")
        token = await self.repository_token(
            full_name, {"contents": "write", "pull_requests": "write"}
        )
        try:
            merged = await self._request(
                "PUT",
                f"/repos/{full_name}/pulls/{number}/merge",
                token=token,
                not_found=f"{full_name} has no pull request #{number}",
                explain=True,
                json={"sha": sha, "merge_method": method},
            )
        finally:
            await self.revoke(token)
        made = merged.get("sha") if isinstance(merged, dict) else None
        if not isinstance(merged, dict) or merged.get("merged") is not True:
            raise GitHubAppError("GitHub did not say that the pull request is merged")
        return str(made or "")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        not_found: str = "GitHub does not know this installation of the app",
        gone: Any = None,
        empty: bool = False,
        explain: bool = False,
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
            said = ""
            if explain:
                # Why GitHub refused, in its own words: what a person has to remove.
                with contextlib.suppress(ValueError):
                    body = response.json()
                    if isinstance(body, dict) and isinstance(body.get("message"), str):
                        said = ": " + " ".join(body["message"].split())[:300]
            raise GitHubAppError(f"GitHub answered {response.status_code}{said}")
        if empty:
            return None
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
