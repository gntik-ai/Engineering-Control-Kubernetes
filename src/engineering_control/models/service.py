"""Asking a model: which account, which provider, how often, and what it cost.

One question gets one answer of the shape the platform states, or a reason why not. The
question names the providers and models it may be asked at, most wanted first. Here an
account of the provider is taken (the one unused longest that has a key and is not
paused), the provider is asked, and what it answers is checked:

- an answer that does not have the shape is asked for once more, saying what was wrong;
- a provider that is over its limit or does not answer is tried again after a moment,
  then the account rests for a few minutes and the next account or provider is tried;
- a provider that does not accept the key marks the account as needing a new key.

The key is opened here, for the call, and goes nowhere else.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import ProviderAccount
from engineering_control.models import anthropic_api, decisions, openai_style, openrouter, shape
from engineering_control.models.pricelist import PriceLists
from engineering_control.models.providers import BY_KEY, ApiProvider, cost_of
from engineering_control.models.sealing import SealError, unseal
from engineering_control.models.wire import TextUpdate, Wire, WireError

LOG = structlog.get_logger(__name__)
# How long a provider that is busy is waited for before it is asked again, and how long
# an account rests once those tries are used up.
BUSY_WAITS: Final = (3.0, 15.0)
REST_MINUTES: Final = 5
MAX_WAIT: Final = 60.0
# The name a step's one question has, where questions are told apart by name.
ONE: Final = "question"


@dataclass(frozen=True)
class Question:
    # Where it may be asked, most wanted first: (provider, model).
    choices: tuple[tuple[str, str], ...]
    system: str
    prompt: str
    schema: dict[str, Any]
    max_tokens: int = 8000
    seconds: int = 300
    effort: str | None = None
    # The prompt begins with what the questions of a run have in common and ends with
    # the question itself. ``cuts`` are the offsets at which a new piece of it begins;
    # ``keep`` the offsets at which something ends that a later question is likely to
    # begin with again; ``shared`` where the question itself begins.
    cuts: tuple[int, ...] = ()
    keep: tuple[int, ...] = ()
    shared: int = 0
    # Whether a provider that caches only what a request marks is asked to: "none",
    # "short" (kept for minutes) or "hour".
    cache: str = "none"
    # The account the run asked with before: a provider's cache belongs to one account.
    prefer: UUID | None = None
    # The shape of the answer is only said in the question, not given to the provider's
    # API to hold the model to. A provider reads a shape it is given before everything
    # else, so questions with different shapes share nothing it kept; said in the
    # question, at its end, the shape leaves the beginning the same. The platform
    # checks the answer either way.
    loose: bool = False
    # Asked in a batch: handed to the provider and answered within hours, at a lower
    # price. ``hours`` is how long the answer is waited for; ``late`` what happens when
    # it has not come by then: "stop" (the step fails and says so) or "ask" (the
    # question is asked at once, at the full price).
    batch: bool = False
    hours: int = 24
    late: str = "stop"
    on_text: TextUpdate | None = None
    tariff: dict[str, float] | None = None
    native_score: bool | None = None
    upstreams: str = ""
    allow_fallbacks: bool = True


@dataclass(frozen=True)
class Reply:
    data: dict[str, Any]
    text: str
    provider: str
    model: str
    account_id: UUID
    account: str
    # Tokens read, written and read again from the provider's cache, and what that
    # cost when the account says what its model costs.
    usage: dict[str, Any]
    seconds: float
    # How often the model was asked: twice when its first answer could not be used.
    asked: int


class ModelError(Exception):
    """No answer. ``later`` says whether asking again later can help: no account is
    ready right now, or every provider was busy."""

    def __init__(
        self,
        label: str,
        detail: str,
        *,
        later: bool = False,
        metered: Reply | None = None,
        provider_ref: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.label = label
        self.metered = metered
        self.provider_ref = provider_ref
        self.detail = detail
        self.later = later


@dataclass(frozen=True)
class _Held:
    id: UUID
    name: str
    provider: ApiProvider
    url: str
    key: str
    prices: dict[str, Any]
    catalog: dict[str, Any] | None = None


# Below this many characters nothing is marked to be kept: no provider caches so
# little, and a mark would only cost.
KEEP_CHARACTERS: Final = 2000


def marks(provider: ApiProvider, question: Question) -> tuple[int, ...]:
    """Where the provider is asked to keep what it read: only a provider that caches
    what a request marks, and only when the question wants it kept."""
    if provider.cache != "marked" or question.cache == "none":
        return ()
    return tuple(end for end in question.keep if end >= KEEP_CHARACTERS)


def usage_of(
    held: _Held,
    model: str,
    answered: str,
    used: dict[str, int],
    question: Question,
    *,
    batch: bool = False,
) -> dict[str, Any]:
    """What a question used, as it is kept with its step: tokens, and what they cost
    when the account says what its model costs."""
    write = held.provider.cache_write[1 if question.cache == "hour" else 0]
    cost = cost_of(
        {
            model: question.tariff
            or (openrouter.DIRECT_PRICE if held.provider.key == "openai" else {})
        }
        if question.native_score is not None
        else held.prices,
        model,
        used,
        write=write,
        part=held.provider.batch_price if batch else 1.0,
        precision=12
        if question.native_score is not None or held.provider.key == "openrouter"
        else 6,
    )
    counted = {name: count for name, count in used.items() if name != "cache_write" or count}
    return {
        "tokens": used["input"]
        + used["output"]
        + (
            used["cached"]
            if question.native_score is not None or held.provider.key == "openrouter"
            else 0
        ),
        **counted,
        **({"cost": cost} if cost is not None else {}),
        **({"batch": True} if batch else {}),
        "provider": held.provider.key,
        "model": answered,
        "account": held.name,
        "account_id": str(held.id),
    }


def with_shape(question: Question) -> str:
    """The question as it is put to a model: its prompt, and the shape of the answer
    said after it."""
    return (
        f"{question.prompt}\n\nAnswer with one JSON object of exactly this shape and "
        f"nothing else, no text before or after it:\n{shape.words(question.schema)}"
    )


def complaint(base: str, why: str) -> str:
    return (
        f"{base}\n\nAn earlier answer could not be used: {why}. Answer "
        "again with only the JSON object."
    )


def names(choices: tuple[tuple[str, str], ...]) -> str:
    said = [f"{BY_KEY[p].name if p in BY_KEY else p} ({m})" for p, m in choices]
    return " or ".join(said)


class Models:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        sealing_key: bytes | None,
        *,
        http: httpx.AsyncClient | None = None,
        anthropic_http: anthropic_api.HttpFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessions = sessions
        self.sealing_key = sealing_key
        self._http = http
        self._anthropic_http = anthropic_http
        self._sleep = sleep
        self._catalog_lock = asyncio.Lock()
        # What the providers' models cost, as public lists say it.
        self.price_lists = PriceLists(lambda: self.http)

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=False)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # --- accounts ------------------------------------------------------------------------

    def _held(
        self,
        provider: str,
        besides: set[UUID],
        prefer: UUID | None = None,
        model: str | None = None,
    ) -> _Held | None:
        """The account of that provider to ask with: the one the run asked with before,
        or else the one unused longest, that has a key, is not paused and is not
        resting."""
        kind = BY_KEY.get(provider)
        if kind is None or self.sealing_key is None:
            return None
        now = datetime.now(UTC)
        with self.sessions() as session:
            accounts = session.scalars(
                select(ProviderAccount).where(
                    ProviderAccount.provider == provider,
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.paused.is_(False),
                    ProviderAccount.sealed_key.is_not(None),
                    ProviderAccount.state.in_(("ready", "unchecked", "limited")),
                )
            ).all()
            ready = [
                account
                for account in accounts
                if account.id not in besides
                and (not model or model in (account.models or []))
                and not (
                    account.state == "limited"
                    and account.limited_until is not None
                    and _aware(account.limited_until) > now
                )
            ]
            ready.sort(
                key=lambda a: (
                    a.id != prefer,
                    _aware(a.last_used_at) if a.last_used_at else _EPOCH,
                )
            )
            for account in ready:
                try:
                    key = unseal(self.sealing_key, account.id, str(account.sealed_key))
                except SealError as error:
                    self._learn(account.id, state="sign_in_required", reason=str(error))
                    continue
                return _Held(
                    id=account.id,
                    name=account.name,
                    provider=kind,
                    url=kind.endpoint(account.endpoint).url,
                    key=key,
                    prices=dict(account.prices or {}),
                    catalog=account.model_catalog,
                )
        return None

    def _learn(
        self,
        account_id: UUID,
        *,
        state: str,
        reason: str | None = None,
        rest: bool = False,
        used: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            account = session.get(ProviderAccount, account_id)
            if account is None:
                return
            account.state = state
            account.state_reason = reason[:300] if reason else None
            account.checked_at = now
            account.limited_until = now + timedelta(minutes=REST_MINUTES) if rest else None
            if used:
                account.last_used_at = now

    # --- one call ------------------------------------------------------------------------

    async def _call(
        self, held: _Held, model: str, question: Question, prompt: str, *, retry: bool = True
    ) -> Wire:
        native = question.native_score is not None
        route = None
        output_limit = question.max_tokens
        if held.provider.key == "openrouter":
            record = (held.catalog or {}).get(model, {})
            if not openrouter.supports(
                record,
                decision=native,
                score=bool(question.native_score),
                schema=not question.loose,
            ):
                raise WireError(
                    "configuration", "Refresh Accounts and choose a model that supports this task."
                )
            try:
                route = openrouter.routing(
                    question.upstreams, question.allow_fallbacks, decisions=native
                )
            except ValueError as exc:
                raise WireError("configuration", str(exc)) from exc
            if isinstance(record.get("max_output"), int) and record["max_output"] > 0:
                output_limit = min(output_limit, record["max_output"])
        if question.native_score is not None:
            payload = decisions.body(
                held.provider.key,
                model,
                question.system,
                question.prompt,
                question.native_score,
                route or {},
            )
            return await decisions.complete(
                self.http, held.provider.key, held.url, held.key, payload, question.seconds
            )
        if held.provider.style == "anthropic":
            return await anthropic_api.complete(
                key=held.key,
                base_url=held.url,
                model=model,
                system=question.system,
                prompt=prompt,
                schema=question.schema,
                max_tokens=question.max_tokens,
                seconds=question.seconds,
                effort=question.effort,
                http=self._anthropic_http,
                cuts=question.cuts,
                keep=marks(held.provider, question),
                hour=question.cache == "hour",
                loose=question.loose,
                retry=retry,
                on_text=question.on_text,
            )
        return await openai_style.complete(
            http=self.http,
            name=held.provider.name,
            key=held.key,
            base_url=held.url,
            model=model,
            system=question.system,
            prompt=prompt,
            schema=question.schema,
            max_tokens=output_limit,
            seconds=question.seconds,
            routing=route,
            limit_name=held.provider.limit_name,
            strict=held.provider.shape == "schema" and not question.loose,
            effort=question.effort if held.provider.effort else None,
            model_prefix=held.provider.model_prefix,
            retry=retry,
            on_text=question.on_text,
        )

    async def _patient(
        self, held: _Held, model: str, question: Question, prompt: str, *, retry: bool = True
    ) -> Wire:
        """One call, waiting and asking again while the provider is only busy."""
        for wait in (*BUSY_WAITS, None) if retry else (None,):
            try:
                return await self._call(held, model, question, prompt, retry=retry)
            except WireError as error:
                if error.kind != "busy" or wait is None:
                    raise
                await self._sleep(min(MAX_WAIT, max(wait, error.retry_after or 0.0)))
        raise AssertionError("unreachable")  # pragma: no cover

    # --- a question ----------------------------------------------------------------------

    async def ask(
        self, question: Question, earlier: str | None = None, *, retry: bool = True
    ) -> Reply:
        """Ask the question at once. ``earlier`` says why an answer it already got (in a
        batch) could not be used: it is then asked once, saying so. With retries
        disabled, at most one inference request is sent across all accounts."""
        if self.sealing_key is None:
            raise ModelError(
                "no_key",
                "This installation has no key to open API keys with, so no model can be asked.",
            )
        base = with_shape(question)
        busy: list[str] = []
        had_account = False
        if question.native_score is not None:
            retry = False
        for provider, model in question.choices:
            tried: set[UUID] = set()
            while True:
                held = await asyncio.to_thread(
                    self._held,
                    provider,
                    tried,
                    question.prefer,
                    model
                    if provider == "openrouter" or question.native_score is not None
                    else None,
                )
                if held is None:
                    break
                tried.add(held.id)
                had_account = True
                started = time.monotonic()
                charges: list[float] = []
                estimated = 0.0
                unpriced_tokens = 0
                used = {"input": 0, "output": 0, "cached": 0, "cache_write": 0}
                prompt, why = (complaint(base, earlier), earlier) if earlier else (base, "")
                try:
                    attempts = (2,) if earlier else ((1, 2) if retry else (1,))
                    for responses, asked in enumerate(attempts, start=1):
                        stopped = None
                        try:
                            wire = await self._patient(held, model, question, prompt, retry=retry)
                        except WireError as exc:
                            if exc.metered is None:
                                raise
                            stopped, wire = exc, exc.metered
                        used["input"] += wire.read
                        used["output"] += wire.written
                        used["cached"] += wire.cached
                        used["cache_write"] += wire.cache_written
                        if provider == "openrouter":
                            used["responses"] = responses
                            if wire.reported_cost is not None:
                                charges.append(wire.reported_cost)
                            else:
                                rates = (
                                    {model: question.tariff}
                                    if question.native_score is not None
                                    else held.prices
                                )
                                cost = cost_of(
                                    rates,
                                    model,
                                    {
                                        "input": wire.read,
                                        "output": wire.written,
                                        "cached": wire.cached,
                                        "cache_write": wire.cache_written,
                                    },
                                    precision=12,
                                )
                                if cost is None:
                                    unpriced_tokens += wire.read + wire.written + wire.cached
                                else:
                                    estimated += cost
                        data = shape.read_object(wire.text)
                        why = (
                            "it was not a JSON object"
                            if data is None
                            else shape.problem(question.schema, data) or ""
                        )
                        reply = Reply(
                            data=data if data is not None and not why else {},
                            text="" if why else wire.text,
                            provider=provider,
                            model=wire.model,
                            account_id=held.id,
                            account=held.name,
                            usage={
                                **usage_of(held, model, wire.model, used, question),
                                **({"reported_credits_usd": sum(charges)} if charges else {}),
                                **(
                                    {
                                        "cost": sum(charges) + estimated,
                                        "unpriced_tokens": unpriced_tokens,
                                    }
                                    if provider == "openrouter"
                                    else {}
                                ),
                                **({"upstream": wire.upstream} if wire.upstream else {}),
                                **(
                                    {
                                        "requested_model": model,
                                        "endpoint": (
                                            openrouter.DECISIONS
                                            if provider == "openrouter"
                                            else openrouter.DIRECT_ENDPOINT
                                        )
                                        if question.native_score is not None
                                        else openrouter.CHAT,
                                        "upstream_policy": {
                                            "only": question.upstreams,
                                            "allow_fallbacks": question.allow_fallbacks,
                                        },
                                    }
                                    if provider == "openrouter" or question.native_score is not None
                                    else {}
                                ),
                                **(
                                    {"provider_ref": wire.provider_ref} if wire.provider_ref else {}
                                ),
                                **({"responses": responses} if responses > 1 else {}),
                            },
                            seconds=round(time.monotonic() - started, 1),
                            asked=asked,
                        )
                        if stopped:
                            await asyncio.to_thread(self._learn, held.id, state="ready", used=True)
                            raise ModelError(stopped.kind, stopped.detail, metered=reply)
                        if not why:
                            await asyncio.to_thread(self._learn, held.id, state="ready", used=True)
                            return reply
                        prompt = complaint(base, why)
                except WireError as error:
                    if error.kind == "refused":
                        await asyncio.to_thread(
                            self._learn, held.id, state="sign_in_required", reason=error.detail
                        )
                        LOG.warning("model_key_refused", account=held.name)
                    elif error.kind == "busy":
                        await asyncio.to_thread(
                            self._learn, held.id, state="limited", reason=error.detail, rest=True
                        )
                        busy.append(error.detail)
                    else:
                        # Another account cannot repair a declined or truncated request.
                        await asyncio.to_thread(self._learn, held.id, state="ready", used=True)
                        raise ModelError(
                            error.kind, error.detail, provider_ref=error.provider_ref
                        ) from error
                    if not retry:
                        raise ModelError(
                            error.kind, error.detail, later=True, provider_ref=error.provider_ref
                        ) from error
                    continue
                await asyncio.to_thread(self._learn, held.id, state="ready", used=True)
                raise ModelError(
                    "no_answer",
                    f"{held.provider.name} ({model}) gave no answer of the asked shape"
                    f"{' twice' if retry else ''}: {why}.",
                    metered=reply,
                )
        if busy:
            raise ModelError("busy", busy[-1], later=True)
        if had_account:
            raise ModelError(
                "no_account",
                f"No account of {names(question.choices)} accepts its key any more.",
                later=True,
            )
        raise ModelError(
            "no_account",
            f"Waiting for an account of {names(question.choices)} that has a key and is ready.",
            later=True,
        )

    # --- trying the cache ------------------------------------------------------------------

    async def try_cache(self, account_id: UUID, model: str) -> dict[str, Any]:
        """Ask a model of the account the same long question until the provider reads
        its beginning from its cache, three times at most, and once more with another
        shape of answer. Says what each time read new, wrote to the cache and read from
        it: whether the provider's cache works for the way the platform asks. It costs
        what a few thousand tokens cost."""
        held = await asyncio.to_thread(self._one, account_id)
        seed = uuid4().hex
        # A beginning no question had before, so that the first time finds nothing kept.
        lines = "\n".join(
            f"{number}. Entry {seed}-{number} of the register is {number * 7919 % 9973}."
            for number in range(1, TRIAL_LINES + 1)
        )
        material = f"A register, made for this one trial ({seed}):\n{lines}\n\n"
        ask = "How many entries does the register have? Answer with the number."

        def question(schema: dict[str, Any], loose: bool = False) -> Question:
            prompt = (
                f"{material}{ask}\n\nAnswer with one JSON object of exactly this shape and "
                f"nothing else, no text before or after it:\n{shape.words(schema)}"
            )
            return Question(
                choices=((held.provider.key, model),),
                system="You answer one question about the text you are given, shortly.",
                prompt=prompt,
                schema=schema,
                max_tokens=4000,
                seconds=120,
                effort="low" if held.provider.effort else None,
                cuts=(len(material),),
                keep=(len(material),),
                shared=len(material),
                cache="short",
                loose=loose,
            )

        def number(name: str) -> dict[str, Any]:
            return {
                "type": "object",
                "additionalProperties": False,
                "required": [name],
                "properties": {name: {"type": "integer"}},
            }

        asked: list[dict[str, Any]] = []
        try:
            for schema in (number("entries"), number("entries"), number("entries")):
                first = question(schema)
                wire = await self._patient(held, model, first, first.prompt)
                asked.append(_trial(wire, "the same question"))
                if wire.cached:
                    break
                await self._sleep(TRIAL_PAUSE)
            other = question(number("count"))
            wire = await self._patient(held, model, other, other.prompt)
            asked.append(_trial(wire, "the same beginning, another shape of answer"))
            # The same again with the shapes only said in the question: the first
            # writes what the second, of another shape, should then read.
            for name in ("entries", "count"):
                said = question(number(name), loose=True)
                wire = await self._patient(held, model, said, said.prompt)
                asked.append(_trial(wire, f"the shape only said in the question ({name})"))
                await self._sleep(TRIAL_PAUSE)
        except WireError as error:
            if error.kind == "refused":
                await asyncio.to_thread(
                    self._learn, held.id, state="sign_in_required", reason=error.detail
                )
            raise ModelError(error.kind, error.detail) from error
        await asyncio.to_thread(self._learn, held.id, state="ready", used=True)
        again = [entry for entry in asked[1:-3] if entry["cached"]]
        return {
            "account": held.name,
            "provider": held.provider.key,
            "model": model,
            "how": held.provider.cache,
            "asked": asked,
            "works": bool(again),
            "with_another_shape": bool(asked[-3]["cached"]),
            "when_only_said": bool(asked[-1]["cached"]),
        }

    def _one(self, account_id: UUID) -> _Held:
        """That account, with its key open. Raises ``ModelError`` when it cannot ask."""
        with self.sessions() as session:
            account = session.get(ProviderAccount, account_id)
            kind = BY_KEY.get(account.provider) if account is not None else None
            if account is None or kind is None or not account.sealed_key:
                raise ModelError("no_account", "That account has no API key.")
            if self.sealing_key is None:
                raise ModelError("no_key", "This installation has no key to open API keys with.")
            try:
                key = unseal(self.sealing_key, account.id, str(account.sealed_key))
            except SealError as error:
                raise ModelError("refused", str(error)) from error
            return _Held(
                id=account.id,
                name=account.name,
                provider=kind,
                url=kind.endpoint(account.endpoint).url,
                key=key,
                prices=dict(account.prices or {}),
                catalog=account.model_catalog,
            )

    # --- trying an account ----------------------------------------------------------------

    def _catalog_accounts(self) -> list[UUID]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(ProviderAccount.id).where(
                        ProviderAccount.provider.in_(tuple(BY_KEY)),
                        ProviderAccount.sealed_key.is_not(None),
                        ProviderAccount.archived_at.is_(None),
                        ProviderAccount.paused.is_(False),
                        ProviderAccount.state.in_(("ready", "limited", "unchecked")),
                    )
                )
            )

    async def refresh_catalogs(self) -> dict[str, int]:
        """Refresh what configured keys can see using only model-list requests."""
        checked = failed = 0
        async with self._catalog_lock:
            for account_id in await asyncio.to_thread(self._catalog_accounts):
                state, _ = await self.check(account_id, catalog_only=True)
                if state == "ready":
                    checked += 1
                else:
                    failed += 1
        return {"refreshed": checked, "unavailable": failed}

    async def check(
        self, account_id: UUID, *, catalog_only: bool = False
    ) -> tuple[str, str | None]:
        """Ask the provider which models the account's key reaches. That costs nothing,
        and says whether the key is accepted. Returns the state and the reason."""

        def read() -> tuple[ProviderAccount | None, bytes | None]:
            with self.sessions() as session:
                account = session.get(ProviderAccount, account_id)
                if account is not None:
                    session.expunge(account)
                return account, self.sealing_key

        account, sealing_key = await asyncio.to_thread(read)
        kind = BY_KEY.get(account.provider) if account is not None else None
        if account is None or kind is None or not account.sealed_key:
            return "signed_out", "The account has no key yet."
        if sealing_key is None:
            return "unchecked", "This installation has no key to open API keys with."
        if account.archived_at is not None or (catalog_only and account.paused):
            return "unchecked", "The account is inactive."
        catalog: dict[str, Any] | None = None
        found: list[str] | None = None
        reason: str | None
        try:
            key = unseal(sealing_key, account.id, account.sealed_key)
            url = kind.endpoint(account.endpoint).url
            if kind.key == "openrouter":
                catalog = await openrouter.catalog(self.http, url, key)
                found = sorted(catalog)
            elif kind.style == "anthropic":
                found = await anthropic_api.models(key=key, base_url=url, http=self._anthropic_http)
            else:
                found = await openai_style.models(
                    http=self.http,
                    name=kind.name,
                    key=key,
                    base_url=url,
                    model_prefix=kind.model_prefix,
                )
        except SealError as error:
            state, reason = "sign_in_required", str(error)
        except WireError as error:
            state = "sign_in_required" if error.kind == "refused" else "unchecked"
            reason = error.detail
        else:
            state, reason = "ready", None

        def save() -> bool:
            with self.sessions.begin() as session:
                current = session.get(ProviderAccount, account_id, with_for_update=True)
                if (
                    current is None
                    or current.archived_at is not None
                    or current.credential_generation != account.credential_generation
                    or current.endpoint != account.endpoint
                    or (
                        catalog_only
                        and (
                            current.paused or current.state not in ("ready", "limited", "unchecked")
                        )
                    )
                ):
                    return False
                if not catalog_only or state == "sign_in_required":
                    current.state, current.state_reason = state, reason[:300] if reason else None
                    current.checked_at = datetime.now(UTC)
                    current.limited_until = None
                current.catalog_error = reason[:300] if reason else None
                if found is not None:
                    current.models = found
                    current.catalog_updated_at = datetime.now(UTC)
                    if catalog is not None:
                        current.model_catalog = catalog
                        prices = dict(current.prices or {})
                        for name, item in catalog.items():
                            if not prices.get(name, {}).get("own"):
                                if item["rates"] is None:
                                    prices.pop(name, None)
                                else:
                                    prices[name] = item["rates"]
                        current.prices = prices
                    current.identity = f"{len(found)} models" if found else None
                return True

        if not await asyncio.to_thread(save):
            return "unchecked", "The account changed while the model list was being read."
        return state, reason


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
# The trial of a cache: how long its text is, in lines of about a dozen tokens each (more
# than any provider's least for caching), and how long is waited between two tries.
TRIAL_LINES: Final = 700
TRIAL_PAUSE: Final = 3.0


def _trial(wire: Wire, what: str) -> dict[str, Any]:
    return {
        "what": what,
        "input": wire.read,
        "cache_write": wire.cache_written,
        "cached": wire.cached,
        "output": wire.written,
    }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
