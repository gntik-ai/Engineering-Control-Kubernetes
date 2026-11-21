"""Accounts: the sign-ins the agents work with, and the keys models are asked with.

An account is of one of two kinds. A subscription account is one sign-in of Claude or
ChatGPT: what the provider's own command-line client produces when someone signs in to a
subscription. An agent in a sandbox works with it, and anything that looks like an API
key is refused for it. An API account is a key of a provider that is paid by use
(Anthropic, OpenAI, DeepSeek, Qwen, GLM, Kimi): the platform asks a model with it
directly, and no sandbox ever holds it.

A subscription's credential is not kept here. It is checked, described (which address,
which plan, as far as it says so itself) and handed to the cluster as a Secret named
after the account. An API key is kept sealed in the account's record
(``models/sealing.py``).
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import ProviderAccount
from engineering_control.models.pricelist import PriceList, renewed
from engineering_control.models.providers import API_PROVIDERS, clean_prices, host_of
from engineering_control.models.providers import BY_KEY as API_BY_KEY

NAME_MAX = 60
MAX_ACCOUNTS = 200
MAX_CREDENTIAL_BYTES = 64 * 1024
# What the platform last learned about an account.
STATES: Final = ("signed_out", "unchecked", "ready", "limited", "sign_in_required")


@dataclass(frozen=True)
class Credential:
    """A checked sign-in: what goes into the Secret, and what may be shown about it."""

    data: dict[str, bytes]
    identity: str | None


@dataclass(frozen=True)
class ProviderType:
    key: str
    name: str
    # The command-line client that works with the subscription.
    client: str | None
    # What the administrator pastes: a token, or the client's sign-in file.
    credential: str | None
    available: bool
    # How to get the credential, step by step, or why the provider is not available yet.
    steps: tuple[str, ...]
    note: str
    # "subscription": an agent's client is signed in with it. "api": a key, paid by use.
    kind: str = "subscription"

    def as_dict(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "client": self.client,
            "credential": self.credential,
            "available": self.available,
            "steps": list(self.steps),
            "note": self.note,
            "kind": self.kind,
        }
        api = API_BY_KEY.get(self.key)
        if api is not None:
            said = api.as_dict()
            found["endpoints"] = said["endpoints"]
            # How it keeps what it read, and whether it takes questions in a batch.
            found["cache"], found["batch"] = said["cache"], said["batch"]
            found["batch_price"] = said["batch_price"]
        return found


def _api(provider: Any) -> ProviderType:
    return ProviderType(
        key=provider.key,
        name=provider.name,
        client=None,
        credential="api_key",
        available=True,
        steps=(
            f"Make an API key at {provider.keys_at}.",
            "Paste the key below. It is kept sealed and is never given to a sandbox.",
        ),
        note=(
            "An API key, paid by use. A model is asked with it directly by the platform; "
            "what a call cost is shown when the account says what its models cost."
        ),
        kind="api",
    )


PROVIDERS: Final = (
    ProviderType(
        key="claude",
        name="Claude",
        client="Claude Code",
        credential="token",
        available=True,
        steps=(
            "On a computer where Claude Code is installed, run: claude setup-token",
            "Sign in with the Claude subscription in the browser window it opens.",
            "Copy the token it prints (it starts with sk-ant-oat) and paste it below.",
        ),
        note=(
            "A token of the subscription, valid for a year. It is not an API key and is not "
            "billed per use."
        ),
    ),
    ProviderType(
        key="chatgpt",
        name="ChatGPT",
        client="Codex",
        credential="auth_file",
        available=True,
        steps=(
            "On a computer where Codex is installed, run: codex login",
            "Sign in with the ChatGPT subscription in the browser window it opens.",
            "Paste below the whole content of the file ~/.codex/auth.json.",
            "If that file does not exist, the sign-in went to the system keyring: run "
            "codex -c cli_auth_credentials_store=file login and look again.",
        ),
        note="The sign-in file of the subscription. Codex renews it by itself while it is used.",
    ),
    *(_api(provider) for provider in API_PROVIDERS),
)
_BY_KEY: Final = {provider.key: provider for provider in PROVIDERS}

# A Claude subscription token, as `claude setup-token` prints it.
_CLAUDE_TOKEN = re.compile(r"^sk-ant-oat\d{2}-[A-Za-z0-9_-]{40,400}$")
# Keys that are billed per use: never accepted, whatever the provider.
_API_KEY = re.compile(r"^(sk-ant-api|sk-proj-|sk-svcacct-|sk-admin-|sk-[A-Za-z0-9]{20,}$)")


def provider_catalogue() -> list[dict[str, Any]]:
    return [provider.as_dict() for provider in PROVIDERS]


def secret_name(account_id: UUID) -> str:
    """The Secret of the sandbox namespace that holds the account's sign-in."""
    return f"account-{account_id}"


def clean_name(value: str) -> str:
    text = " ".join(unicodedata.normalize("NFC", value or "").split())
    if not text:
        raise ProjectInvalid("The account needs a name")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ProjectInvalid("The name holds a control character")
    if len(text) > NAME_MAX:
        raise ProjectInvalid(f"The name is longer than {NAME_MAX} characters")
    return text


def _claims(token: str) -> dict[str, Any]:
    """The readable middle part of a signed token. Not verified: it is only described."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def _printable(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()
    return text[:limit] or None


def check_credential(provider: str, pasted: str) -> Credential:
    """What the administrator pasted, checked and reduced to what the sandbox needs.

    Raises ``ProjectInvalid`` with a sentence the administrator can act on. The pasted text
    is never part of that sentence.
    """
    kind = _BY_KEY[provider]
    if not kind.available:
        raise ProjectInvalid(f"{kind.name} accounts cannot be signed in yet")
    if kind.kind == "api":
        raise ProjectInvalid(f"{kind.name} takes an API key, not a subscription sign-in")
    text = (pasted or "").strip()
    if not text:
        raise ProjectInvalid("Nothing was pasted")
    if len(text.encode()) > MAX_CREDENTIAL_BYTES:
        raise ProjectInvalid("What was pasted is too long to be a sign-in")
    if _API_KEY.match(text):
        raise ProjectInvalid(
            "That is an API key. Accounts work with a subscription sign-in only; see the steps."
        )
    if kind.credential == "token":
        if not _CLAUDE_TOKEN.fullmatch(text):
            raise ProjectInvalid(
                "That is not a token from claude setup-token: it starts with sk-ant-oat "
                "and is one line"
            )
        return Credential(data={"token": text.encode("ascii")}, identity=None)
    # The sign-in file of Codex.
    try:
        document = json.loads(text)
    except ValueError as error:
        raise ProjectInvalid(
            "That is not the content of auth.json: paste the whole file, from { to }"
        ) from error
    if not isinstance(document, dict):
        raise ProjectInvalid("That is not the content of auth.json: paste the whole file")
    if document.get("OPENAI_API_KEY"):
        raise ProjectInvalid(
            "This sign-in file holds an API key. Sign in to the subscription with codex "
            "login, without an API key, and paste the new file."
        )
    tokens = document.get("tokens")
    if not isinstance(tokens, dict) or not all(
        isinstance(tokens.get(part), str) and tokens[part]
        for part in ("access_token", "refresh_token")
    ):
        raise ProjectInvalid(
            "This sign-in file holds no subscription sign-in (no tokens). Run codex login "
            "again and paste the new file."
        )
    claims = _claims(tokens["id_token"]) if isinstance(tokens.get("id_token"), str) else {}
    profile = claims.get("https://api.openai.com/auth")
    plan = _printable(profile.get("chatgpt_plan_type"), 30) if isinstance(profile, dict) else None
    address = _printable(claims.get("email"), 120)
    identity = " · ".join(part for part in (address, f"plan {plan}" if plan else None) if part)
    # Stored as it was pasted, re-serialised: Codex reads and rewrites this very file.
    stored = json.dumps(document, separators=(",", ":")).encode()
    return Credential(data={"auth.json": stored}, identity=identity or None)


def _when(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _written(now: dict[str, Any], wanted: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Prices as the administrator wrote them. One they changed or added is theirs
    (``own``): it is not replaced when the list prices are read again. One they left
    as the list had it stays the list's."""
    kept: dict[str, Any] = {}
    for model, entry in wanted.items():
        before = now.get(model)
        same = isinstance(before, dict) and all(
            before.get(part) == entry.get(part) for part in ("input", "output", "cached")
        )
        own = bool(before.get("own")) if same and isinstance(before, dict) else True
        extras = {
            k: before[k]
            for k in ("request", "cache_write")
            if isinstance(before, dict) and k in before
        }
        kept[model] = {**extras, **entry, **({"own": True} if own else {})}
    return kept


def _row(account: ProviderAccount) -> dict[str, Any]:
    kind = _BY_KEY.get(account.provider)
    api = API_BY_KEY.get(account.provider)
    of_api: dict[str, Any] = {}
    if api is not None:
        endpoint = api.endpoint(account.endpoint)
        of_api = {
            "endpoint": endpoint.key,
            "endpoint_name": endpoint.name,
            "host": host_of(endpoint.url),
            "models": list(account.models or []),
            "catalog_error": account.catalog_error,
            "catalog_updated_at": _when(account.catalog_updated_at),
            "decision_models": sum(
                "decisions" in entry.get("outputs", [])
                for entry in (account.model_catalog or {}).values()
            ),
            "prices": dict(account.prices or {}),
        }
    return {
        **of_api,
        "kind": kind.kind if kind else "subscription",
        "id": str(account.id),
        "name": account.name,
        "provider": account.provider,
        "provider_name": kind.name if kind else account.provider,
        "client": kind.client if kind else None,
        "state": account.state,
        "state_reason": account.state_reason,
        "paused": account.paused,
        "max_concurrent": account.max_concurrent,
        "identity": account.identity,
        "signed_in_at": _when(account.signed_in_at),
        "checked_at": _when(account.checked_at),
        "last_used_at": _when(account.last_used_at),
        "limited_until": _when(account.limited_until),
        "created_at": _when(account.created_at),
        # A row for the activity record and for the console's lists.
        "account": account.name,
    }


class Accounts:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _one(session: Session, account_id: UUID, *, lock: bool = False) -> ProviderAccount:
        query = select(ProviderAccount).where(
            ProviderAccount.id == account_id, ProviderAccount.archived_at.is_(None)
        )
        account = session.scalar(query.with_for_update() if lock else query)
        if account is None:
            raise ProjectNotFound(str(account_id))
        return account

    @staticmethod
    def _name_free(session: Session, name: str, *, besides: UUID | None = None) -> None:
        taken = session.scalar(
            select(ProviderAccount.id).where(
                func.lower(ProviderAccount.name) == name.lower(),
                ProviderAccount.archived_at.is_(None),
            )
        )
        if taken is not None and taken != besides:
            raise ProjectConflict(f"Another account is already named {name}")

    def listing(self) -> dict[str, Any]:
        with self.sessions() as session:
            accounts = session.scalars(
                select(ProviderAccount)
                .where(ProviderAccount.archived_at.is_(None))
                .order_by(ProviderAccount.provider, func.lower(ProviderAccount.name))
            ).all()
            return {"items": [_row(account) for account in accounts], "total": len(accounts)}

    def get(self, account_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            return _row(self._one(session, account_id))

    def create(self, *, name: str, provider: str, endpoint: str | None = None) -> dict[str, Any]:
        kind = _BY_KEY.get(provider)
        if kind is None:
            raise ProjectInvalid("That provider is not known")
        if not kind.available:
            raise ProjectInvalid(f"{kind.name} accounts cannot be added yet")
        cleaned = clean_name(name)
        api = API_BY_KEY.get(provider)
        at = self._endpoint(provider, endpoint) if api is not None else None
        with self.sessions.begin() as session:
            self._name_free(session, cleaned)
            total = session.scalar(
                select(func.count())
                .select_from(ProviderAccount)
                .where(ProviderAccount.archived_at.is_(None))
            )
            if (total or 0) >= MAX_ACCOUNTS:
                raise ProjectInvalid(f"There are already {MAX_ACCOUNTS} accounts")
            account = ProviderAccount(
                name=cleaned,
                provider=provider,
                state="signed_out",
                state_reason=None,
                paused=False,
                max_concurrent=2,
                credential_generation=0,
                endpoint=at,
            )
            session.add(account)
            session.flush()
            return _row(account)

    def update(
        self,
        account_id: UUID,
        *,
        name: str | None = None,
        max_concurrent: int | None = None,
        paused: bool | None = None,
        endpoint: str | None = None,
        prices: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            account = self._one(session, account_id, lock=True)
            if endpoint is not None or prices is not None:
                if account.provider not in API_BY_KEY:
                    raise ProjectInvalid("Only an account with an API key has an address or prices")
                if endpoint is not None:
                    at = self._endpoint(account.provider, endpoint)
                    if at != account.endpoint and account.sealed_key:
                        # A key belongs to the address it was made for.
                        account.state, account.state_reason = "unchecked", None
                        account.models = None
                        account.model_catalog = account.catalog_error = None
                        account.catalog_updated_at = None
                    account.endpoint = at
                if prices is not None:
                    try:
                        account.prices = (
                            _written(dict(account.prices or {}), clean_prices(prices)) or None
                        )
                    except ValueError as error:
                        raise ProjectInvalid(str(error)) from error
            if name is not None:
                cleaned = clean_name(name)
                self._name_free(session, cleaned, besides=account.id)
                account.name = cleaned
            if max_concurrent is not None:
                if not 1 <= max_concurrent <= 8:
                    raise ProjectInvalid("An account serves between 1 and 8 sandboxes at once")
                account.max_concurrent = max_concurrent
            if paused is not None:
                account.paused = paused
            session.flush()
            return _row(account)

    def list_prices(self, found: PriceList) -> dict[str, Any]:
        """Put the list prices in for every account with an API key: for each model its
        key reaches that the list has a price for. Prices written by hand stay."""
        said: list[dict[str, Any]] = []
        with self.sessions.begin() as session:
            accounts = session.scalars(
                select(ProviderAccount)
                .where(
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.provider.in_(list(API_BY_KEY)),
                )
                .order_by(ProviderAccount.name)
                .with_for_update()
            ).all()
            for account in accounts:
                prices, done = renewed(
                    dict(account.prices or {}),
                    [str(model) for model in account.models or []],
                    functools.partial(found.price, account.provider, account.endpoint),
                )
                if done["added"] or done["changed"]:
                    account.prices = prices or None
                said.append({"name": account.name, "provider": account.provider, **done})
        return {
            "lists": list(found.read),
            "read_at": found.at.isoformat(),
            "accounts": said,
            "changed": sum(entry["added"] + entry["changed"] for entry in said),
            "priced": sum(entry["priced"] for entry in said),
        }

    def signed_in(self, account_id: UUID, identity: str | None) -> dict[str, Any]:
        """The cluster holds a new sign-in for the account; it has not been tried yet."""
        with self.sessions.begin() as session:
            account = self._one(session, account_id, lock=True)
            account.state = "unchecked"
            account.state_reason = None
            account.identity = identity
            account.limited_until = None
            account.signed_in_at = datetime.now(UTC)
            account.credential_generation += 1
            session.flush()
            return _row(account)

    @staticmethod
    def _endpoint(provider: str, endpoint: str | None) -> str:
        api = API_BY_KEY[provider]
        if endpoint in (None, ""):
            return api.endpoints[0].key
        if endpoint not in {entry.key for entry in api.endpoints}:
            raise ProjectInvalid(f"{api.name} has no address {endpoint}")
        return str(endpoint)

    def key_stored(self, account_id: UUID, sealed: str) -> dict[str, Any]:
        """An API key was entered for the account; it has not been tried yet."""
        with self.sessions.begin() as session:
            account = self._one(session, account_id, lock=True)
            if account.provider not in API_BY_KEY:
                raise ProjectInvalid("That account is signed in to a subscription, not given a key")
            account.sealed_key = sealed
            account.state, account.state_reason = "unchecked", None
            account.identity = None
            account.models = None
            account.model_catalog = account.catalog_error = None
            account.catalog_updated_at = None
            account.limited_until = None
            account.signed_in_at = datetime.now(UTC)
            account.credential_generation += 1
            session.flush()
            return _row(account)

    def checked(
        self,
        account_id: UUID,
        *,
        state: str,
        reason: str | None,
        models: list[str] | None = None,
    ) -> dict[str, Any]:
        """What asking the provider with the account's key showed."""
        with self.sessions.begin() as session:
            account = self._one(session, account_id, lock=True)
            account.state = state
            account.state_reason = reason[:300] if reason else None
            account.checked_at = datetime.now(UTC)
            if state == "ready":
                account.limited_until = None
            if models is not None:
                account.models = models
                account.identity = f"{len(models)} models" if models else None
            session.flush()
            return _row(account)

    def archive(self, account_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as session:
            account = self._one(session, account_id, lock=True)
            account.archived_at = datetime.now(UTC)
            account.state = "signed_out"
            account.identity = None
            account.sealed_key = None
            session.flush()
            return _row(account)

    def inbox(self) -> list[dict[str, Any]]:
        """Accounts that wait for the administrator: never signed in, or refused."""
        with self.sessions() as session:
            waiting = session.scalars(
                select(ProviderAccount)
                .where(
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.state.in_(("signed_out", "sign_in_required")),
                )
                .order_by(func.lower(ProviderAccount.name))
            ).all()
            return [
                {
                    "kind": "account_sign_in",
                    "project": None,
                    "project_name": None,
                    "section": None,
                    "count": 1,
                    "detail": account.state_reason,
                    "account": account.name,
                    "account_id": str(account.id),
                }
                for account in waiting
            ]
