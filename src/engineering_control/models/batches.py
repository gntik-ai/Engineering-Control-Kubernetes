"""Questions asked in a batch: handed to a provider, followed for hours, fetched.

Some providers answer a question within hours instead of seconds at a lower price. A
question asked that way is handed in as a batch, and the step that asked it waits:

1. The batch is recorded here first (``submitting``), then handed to the provider, then
   recorded as handed (``waiting``) with the name the provider gave it.
2. It is asked about every minute at first and every five minutes after ten: where it
   is, and how many of its questions are answered.
3. When the provider says it has ended, what it answered is fetched and kept with each
   request, and the batch is ``ended``.
4. When its time is over, or its run is stopped, the provider is asked to stop it
   (``stopping``); what it had answered by then is still fetched.

Everything a batch is lives in its records, nothing in memory: a launcher that starts
again goes on from them. A batch that was recorded but is not known to have reached the
provider is looked for there before it is handed in again, so that nothing is paid
twice.

The key is opened here, for each call, and goes nowhere else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import (
    ModelBatch,
    ModelBatchRequest,
    ProviderAccount,
    SandboxRun,
    SandboxStep,
)
from engineering_control.models import anthropic_api, google_batch, openai_batch, openai_style
from engineering_control.models.providers import BY_KEY
from engineering_control.models.service import (
    ModelError,
    Models,
    Question,
    _Held,
    marks,
    names,
    usage_of,
    with_shape,
)
from engineering_control.models.wire import Wire, WireError

LOG = structlog.get_logger(__name__)
# How often a batch is asked about: at first, and once it is older than a while.
CHECK_SOON: Final = 60.0
CHECK_LATER: Final = 300.0
SOON_MINUTES: Final = 10
# For how long a batch that is recorded but not known to be handed in is left alone:
# whoever recorded it may still be handing it in.
HANDING_SECONDS: Final = 120
# How long a batch is waited for when its task says nothing, and at most.
HOURS: Final = 24
OPEN: Final = ("submitting", "waiting", "stopping")


@dataclass(frozen=True)
class Answer:
    """What came back for one question of a batch."""

    name: str
    question: Question
    # "answered", "failed" (the provider could not answer, or the model declined) or
    # "late" (the batch ended before it was answered).
    state: str
    text: str = ""
    model: str = ""
    usage: dict[str, Any] | None = None
    kind: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Followed:
    """Where a step's batch is."""

    # "waiting", "ended" or "failed".
    state: str
    detail: str
    provider: str = ""
    account: str = ""
    account_id: UUID | None = None
    seconds: float = 0.0
    answers: tuple[Answer, ...] = ()


def kept(question: Question) -> dict[str, Any]:
    """A question as it is kept with its request."""
    return {
        "choices": [list(choice) for choice in question.choices],
        "system": question.system,
        "prompt": question.prompt,
        "schema": question.schema,
        "max_tokens": question.max_tokens,
        "seconds": question.seconds,
        "effort": question.effort,
        "cuts": list(question.cuts),
        "keep": list(question.keep),
        "shared": question.shared,
        "cache": question.cache,
        "loose": question.loose,
        "late": question.late,
        "hours": question.hours,
    }


def asked(stored: dict[str, Any]) -> Question:
    return Question(
        choices=tuple((str(p), str(m)) for p, m in stored.get("choices") or []),
        system=str(stored.get("system") or ""),
        prompt=str(stored.get("prompt") or ""),
        schema=dict(stored.get("schema") or {}),
        max_tokens=int(stored.get("max_tokens") or 8000),
        seconds=int(stored.get("seconds") or 300),
        effort=stored.get("effort") or None,
        cuts=tuple(int(cut) for cut in stored.get("cuts") or []),
        keep=tuple(int(end) for end in stored.get("keep") or []),
        shared=int(stored.get("shared") or 0),
        cache=str(stored.get("cache") or "none"),
        loose=bool(stored.get("loose")),
        batch=True,
        late=str(stored.get("late") or "stop"),
        hours=int(stored.get("hours") or HOURS),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _row(batch: ModelBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "step_id": batch.step_id,
        "run_id": batch.run_id,
        "account_id": batch.account_id,
        "provider": batch.provider,
        "model": batch.model,
        "remote_id": batch.remote_id,
        "state": batch.state,
        "stopping": batch.stopping,
        "requests": batch.requests,
        "answered": batch.answered,
        "detail": batch.detail,
        "created_at": _aware(batch.created_at),
        "submitted_at": _aware(batch.submitted_at) if batch.submitted_at else None,
        "checked_at": _aware(batch.checked_at) if batch.checked_at else None,
        "ended_at": _aware(batch.ended_at) if batch.ended_at else None,
        "deadline_at": _aware(batch.deadline_at),
    }


class Batches:
    def __init__(self, models: Models, *, now: Callable[[], datetime] | None = None) -> None:
        self.models = models
        self.sessions = models.sessions
        self._now = now or (lambda: datetime.now(UTC))

    # --- the provider ----------------------------------------------------------------------

    async def _create(
        self, held: _Held, model: str, requests: list[tuple[str, Question]], label: str
    ) -> str:
        how = held.provider.batch
        if how == "anthropic":
            return await anthropic_api.batch_create(
                key=held.key,
                base_url=held.url,
                requests=[
                    (
                        name,
                        anthropic_api.request_of(
                            model=model,
                            system=question.system,
                            prompt=question.prompt,
                            schema=question.schema,
                            max_tokens=question.max_tokens,
                            effort=question.effort,
                            cuts=question.cuts,
                            keep=marks(held.provider, question),
                            hour=question.cache == "hour",
                            loose=question.loose,
                        ),
                    )
                    for name, question in requests
                ],
                http=self.models._anthropic_http,
            )
        if how == "files":
            return await openai_batch.create(
                http=self.models.http,
                name=held.provider.name,
                key=held.key,
                base_url=held.url,
                requests=[
                    (
                        name,
                        openai_style.body_of(
                            model=model,
                            system=question.system,
                            prompt=question.prompt,
                            schema=question.schema,
                            max_tokens=question.max_tokens,
                            limit_name=held.provider.limit_name,
                            strict=held.provider.shape == "schema" and not question.loose,
                            effort=question.effort if held.provider.effort else None,
                        ),
                    )
                    for name, question in requests
                ],
                label=label,
                endpoint=held.provider.batch_endpoint,
            )
        if how == "google":
            return await google_batch.create(
                http=self.models.http,
                key=held.key,
                base_url=held.url,
                model=model,
                requests=[
                    (
                        name,
                        google_batch.request_of(
                            system=question.system,
                            prompt=question.prompt,
                            max_tokens=question.max_tokens,
                        ),
                    )
                    for name, question in requests
                ],
                label=label,
            )
        raise WireError("invalid", f"{held.provider.name} takes no questions in a batch.")

    async def _state(self, held: _Held, remote_id: str) -> dict[str, Any]:
        how = held.provider.batch
        if how == "files":
            return await openai_batch.state(
                http=self.models.http,
                name=held.provider.name,
                key=held.key,
                base_url=held.url,
                batch_id=remote_id,
            )
        if how == "google":
            return await google_batch.state(
                http=self.models.http, key=held.key, base_url=held.url, batch_id=remote_id
            )
        return await anthropic_api.batch_state(
            key=held.key, base_url=held.url, batch_id=remote_id, http=self.models._anthropic_http
        )

    async def _recent(self, held: _Held) -> list[dict[str, Any]]:
        how = held.provider.batch
        if how == "files":
            return await openai_batch.recent(
                http=self.models.http, name=held.provider.name, key=held.key, base_url=held.url
            )
        if how == "google":
            return await google_batch.recent(http=self.models.http, key=held.key, base_url=held.url)
        return await anthropic_api.batch_recent(
            key=held.key, base_url=held.url, http=self.models._anthropic_http
        )

    async def _cancel(self, held: _Held, remote_id: str) -> None:
        how = held.provider.batch
        if how == "files":
            await openai_batch.cancel(
                http=self.models.http,
                name=held.provider.name,
                key=held.key,
                base_url=held.url,
                batch_id=remote_id,
            )
        elif how == "google":
            await google_batch.cancel(
                http=self.models.http, key=held.key, base_url=held.url, batch_id=remote_id
            )
        else:
            await anthropic_api.batch_cancel(
                key=held.key,
                base_url=held.url,
                batch_id=remote_id,
                http=self.models._anthropic_http,
            )

    async def _results(
        self,
        held: _Held,
        remote_id: str,
        model: str,
        requests: list[tuple[str, Question]],
        where: dict[str, Any],
    ) -> dict[str, Wire | WireError]:
        """What the batch answered. ``where`` is its state as the provider last gave it:
        some providers say there where the answers are, or give them with it."""
        tokens = {name: question.max_tokens for name, question in requests}
        how = held.provider.batch
        if how == "files":
            return await openai_batch.results(
                http=self.models.http,
                name=held.provider.name,
                key=held.key,
                base_url=held.url,
                where=where,
                model=model,
                tokens=tokens,
                model_prefix=held.provider.model_prefix,
            )
        if how == "google":
            return google_batch.results(where, model, tokens, [name for name, _ in requests])
        return await anthropic_api.batch_results(
            key=held.key,
            base_url=held.url,
            batch_id=remote_id,
            tokens=tokens,
            http=self.models._anthropic_http,
        )

    # --- the records -----------------------------------------------------------------------

    def _record(
        self,
        step_id: UUID | None,
        run_id: UUID | None,
        held: _Held,
        model: str,
        questions: Sequence[tuple[str, Question]],
        hours: int,
    ) -> UUID:
        now = self._now()
        with self.sessions.begin() as session:
            batch = ModelBatch(
                created_at=now,
                step_id=step_id,
                run_id=run_id,
                account_id=held.id,
                provider=held.provider.key,
                model=model,
                state="submitting",
                requests=len(questions),
                answered=0,
                deadline_at=now + timedelta(hours=max(1, min(hours, HOURS))),
            )
            session.add(batch)
            session.flush()
            for position, (name, question) in enumerate(questions):
                session.add(
                    ModelBatchRequest(
                        batch_id=batch.id,
                        name=name,
                        position=position,
                        # The shape of the answer is said in the question, as it is
                        # when the question is asked at once.
                        question=kept(
                            Question(**{**question.__dict__, "prompt": with_shape(question)})
                        ),
                        state="waiting",
                    )
                )
            return batch.id

    def _read(self, batch_id: UUID) -> tuple[dict[str, Any], list[tuple[str, Question]]] | None:
        with self.sessions() as session:
            batch = session.get(ModelBatch, batch_id)
            if batch is None:
                return None
            requests = session.scalars(
                select(ModelBatchRequest)
                .where(ModelBatchRequest.batch_id == batch_id)
                .order_by(ModelBatchRequest.position)
            ).all()
            return _row(batch), [(request.name, asked(request.question)) for request in requests]

    def _latest(self, step_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            batch = session.scalar(
                select(ModelBatch)
                .where(ModelBatch.step_id == step_id)
                .order_by(ModelBatch.created_at.desc())
                .limit(1)
            )
            return _row(batch) if batch is not None else None

    def _change(self, batch_id: UUID, **fields: Any) -> None:
        with self.sessions.begin() as session:
            batch = session.get(ModelBatch, batch_id)
            if batch is None:
                return
            for name, value in fields.items():
                setattr(batch, name, value[:600] if name == "detail" and value else value)

    def _drop(self, batch_id: UUID) -> None:
        with self.sessions.begin() as session:
            batch = session.get(ModelBatch, batch_id)
            if batch is not None:
                session.delete(batch)

    def _known(self) -> set[str]:
        with self.sessions() as session:
            found = session.scalars(
                select(ModelBatch.remote_id).where(ModelBatch.remote_id.is_not(None))
            ).all()
            return {str(name) for name in found}

    def _store(
        self,
        batch: dict[str, Any],
        held: _Held,
        requests: list[tuple[str, Question]],
        results: dict[str, Wire | WireError],
    ) -> None:
        questions = dict(requests)
        now = self._now()
        with self.sessions.begin() as session:
            record = session.get(ModelBatch, batch["id"])
            if record is None:
                return
            answered = 0
            for request in session.scalars(
                select(ModelBatchRequest).where(ModelBatchRequest.batch_id == record.id)
            ):
                result = results.get(request.name)
                if isinstance(result, Wire):
                    used = {
                        "input": result.read,
                        "output": result.written,
                        "cached": result.cached,
                        "cache_write": result.cache_written,
                    }
                    request.state, request.answer = "answered", result.text
                    request.model = (result.model or record.model)[:120]
                    request.usage = usage_of(
                        held,
                        record.model,
                        request.model,
                        used,
                        questions[request.name],
                        batch=True,
                    )
                    answered += 1
                elif result is None or result.kind == "late":
                    request.state, request.kind = "late", "late"
                    request.error = (
                        result.detail
                        if result is not None
                        else "The batch ended without an answer to this."
                    )[:600]
                else:
                    request.state, request.kind = "failed", result.kind[:20]
                    request.error = result.detail[:600]
            record.state, record.ended_at, record.checked_at = "ended", now, now
            record.answered = answered
            if record.stopping == "late":
                record.detail = (
                    f"It was not answered in its time: {answered} of {record.requests} "
                    "questions were answered when it was stopped."
                )
            elif record.stopping:
                record.detail = (
                    f"It was stopped: {answered} of {record.requests} questions were answered."
                )
            else:
                record.detail = f"{answered} of {record.requests} questions were answered."

    # --- handing in ------------------------------------------------------------------------

    async def submit(
        self,
        step_id: UUID | None,
        run_id: UUID | None,
        questions: Sequence[tuple[str, Question]],
        *,
        hours: int = HOURS,
    ) -> UUID:
        """Hand the questions to a provider as one batch. They are all asked at the
        first provider the first of them names that takes batches, with one account.
        Raises ``ModelError``: with ``later`` when it is worth trying again."""
        first = questions[0][1]
        choice = next(
            ((provider, model) for provider, model in first.choices if BY_KEY[provider].batch),
            None,
        )
        if choice is None:
            raise ModelError("no_batch", f"{names(first.choices)} takes no questions in a batch.")
        provider, model = choice
        held = await asyncio.to_thread(self.models._held, provider, set(), first.prefer)
        if held is None:
            raise ModelError(
                "no_account",
                f"Waiting for an account of {BY_KEY[provider].name} that has a key and is ready.",
                later=True,
            )
        batch_id = await asyncio.to_thread(
            self._record, step_id, run_id, held, model, questions, hours
        )
        await self._hand(batch_id, held)
        return batch_id

    async def _hand(self, batch_id: UUID, held: _Held) -> None:
        read = await asyncio.to_thread(self._read, batch_id)
        if read is None:
            return
        batch, requests = read
        try:
            remote = await self._create(held, str(batch["model"]), requests, str(batch_id))
        except WireError as error:
            kind, detail = error.kind, error.detail
            if kind in ("refused", "busy"):
                # Nothing reached the provider: the record goes, and the step tries
                # again, with another account when this one's key is refused.
                await asyncio.to_thread(
                    lambda: self.models._learn(
                        held.id,
                        state="sign_in_required" if kind == "refused" else "limited",
                        reason=detail,
                        rest=kind == "busy",
                    )
                )
                await asyncio.to_thread(self._drop, batch_id)
                raise ModelError(
                    "busy" if kind == "busy" else "no_account", detail, later=True
                ) from error
            await asyncio.to_thread(lambda: self._change(batch_id, state="failed", detail=detail))
            raise ModelError(kind, detail) from error
        now = self._now()
        await asyncio.to_thread(
            lambda: self._change(
                batch_id, remote_id=remote, state="waiting", submitted_at=now, checked_at=now
            )
        )
        await asyncio.to_thread(lambda: self.models._learn(held.id, state="ready", used=True))
        LOG.info("batch_handed_in", provider=held.provider.key, requests=len(requests))

    # --- following -------------------------------------------------------------------------

    def _words(self, batch: dict[str, Any]) -> str:
        name = BY_KEY[batch["provider"]].name if batch["provider"] in BY_KEY else batch["provider"]
        since = batch["submitted_at"] or batch["created_at"]
        said = (
            f"In a batch at {name} since {since:%d %b %H:%M} UTC: {batch['answered']} of "
            f"{batch['requests']} answered."
        )
        if batch["state"] == "stopping":
            return f"{said} It is being stopped."
        return f"{said} It is waited for until {batch['deadline_at']:%d %b %H:%M} UTC."

    def _ended(self, batch: dict[str, Any]) -> Followed:
        with self.sessions() as session:
            requests = session.scalars(
                select(ModelBatchRequest)
                .where(ModelBatchRequest.batch_id == batch["id"])
                .order_by(ModelBatchRequest.position)
            ).all()
            account = (
                session.get(ProviderAccount, batch["account_id"]) if batch["account_id"] else None
            )
            answers = tuple(
                Answer(
                    name=request.name,
                    question=asked(request.question),
                    state=request.state,
                    text=request.answer or "",
                    model=request.model or batch["model"],
                    usage=dict(request.usage) if request.usage else None,
                    kind=request.kind,
                    error=request.error,
                )
                for request in requests
            )
        started = batch["submitted_at"] or batch["created_at"]
        return Followed(
            "ended",
            batch["detail"] or "",
            provider=batch["provider"],
            account=account.name if account is not None else "",
            account_id=batch["account_id"],
            seconds=round(((batch["ended_at"] or started) - started).total_seconds(), 1),
            answers=answers,
        )

    async def follow(self, step_id: UUID) -> Followed | None:
        """Where the step's batch is, asking the provider when it is time to. None when
        the step has no batch."""
        batch = await asyncio.to_thread(self._latest, step_id)
        return await self._follow(batch) if batch is not None else None

    async def _follow(self, batch: dict[str, Any]) -> Followed:
        now = self._now()
        if batch["state"] == "failed":
            return Followed("failed", batch["detail"] or "The batch failed.")
        if batch["state"] == "ended":
            return self._ended(batch)
        try:
            if batch["account_id"] is None:
                raise ModelError("no_account", "The account the batch was handed in with is gone.")
            held = await asyncio.to_thread(self.models._one, batch["account_id"])
        except ModelError as error:
            detail = f"The batch cannot be followed: {error.detail}"
            await asyncio.to_thread(
                lambda: self._change(batch["id"], state="failed", detail=detail)
            )
            return Followed("failed", detail)
        if batch["state"] == "submitting":
            if (now - batch["created_at"]).total_seconds() < HANDING_SECONDS:
                return Followed("waiting", f"The batch is being handed to {held.provider.name}.")
            return await self._recover(batch, held)
        if batch["state"] == "waiting" and now >= batch["deadline_at"]:
            batch = await self._stop(batch, held, "late")
        every = (
            CHECK_SOON
            if (now - (batch["submitted_at"] or batch["created_at"])).total_seconds()
            < SOON_MINUTES * 60
            else CHECK_LATER
        )
        checked = batch["checked_at"]
        if checked is not None and (now - checked).total_seconds() < every:
            return Followed("waiting", self._words(batch))
        try:
            state = await self._state(held, str(batch["remote_id"]))
            if state["ended"]:
                read = await asyncio.to_thread(self._read, batch["id"])
                requests = read[1] if read is not None else []
                results = await self._results(
                    held, str(batch["remote_id"]), str(batch["model"]), requests, state
                )
        except WireError as error:
            # A provider that cannot be asked just now is asked again at the next time.
            await asyncio.to_thread(lambda: self._change(batch["id"], checked_at=now))
            LOG.warning("batch_not_followed", provider=batch["provider"], kind=error.kind)
            return Followed("waiting", f"{self._words(batch)} (It could not be asked about now.)")
        if not state["ended"]:
            await asyncio.to_thread(
                lambda: self._change(batch["id"], checked_at=now, answered=state["succeeded"])
            )
            return Followed("waiting", self._words({**batch, "answered": state["succeeded"]}))
        await asyncio.to_thread(self._store, batch, held, requests, results)
        after = await asyncio.to_thread(self._read, batch["id"])
        LOG.info("batch_ended", provider=batch["provider"], requests=batch["requests"])
        return self._ended(after[0] if after is not None else batch)

    async def _recover(self, batch: dict[str, Any], held: _Held) -> Followed:
        """A batch that was recorded and is not known to have reached the provider: the
        provider is asked what it was handed lately. One batch of that size and time
        that the platform knows nothing of is this one. None: it is handed in now.
        Several: it is not handed in again, since it may be one of them."""
        try:
            recent = await self._recent(held)
        except WireError:
            return Followed("waiting", f"{held.provider.name} could not be asked about the batch.")
        known = await asyncio.to_thread(self._known)
        since = batch["created_at"] - timedelta(seconds=60)
        # A provider that keeps the label a batch was handed in with says which it is.
        labelled = [entry for entry in recent if entry.get("label") == str(batch["id"])]
        found = labelled or [
            entry
            for entry in recent
            if entry["id"] not in known
            and not entry.get("label")
            and _aware(entry["created_at"]) >= since
            and entry.get(
                "total",
                entry["waiting"]
                + entry["succeeded"]
                + entry["errored"]
                + entry["cancelled"]
                + entry["expired"],
            )
            == batch["requests"]
        ]
        if len(found) > 1:
            detail = (
                f"It is not known whether the batch reached {held.provider.name}: "
                f"{len(found)} batches made there at that time could be it. It was not "
                "handed in again, so that nothing is paid twice."
            )
            await asyncio.to_thread(
                lambda: self._change(batch["id"], state="failed", detail=detail)
            )
            return Followed("failed", detail)
        if found:
            now = self._now()
            await asyncio.to_thread(
                lambda: self._change(
                    batch["id"],
                    remote_id=found[0]["id"],
                    state="waiting",
                    submitted_at=_aware(found[0]["created_at"]),
                    checked_at=now,
                )
            )
            LOG.info("batch_found_again", provider=batch["provider"])
            return Followed("waiting", f"The batch was found again at {held.provider.name}.")
        try:
            await self._hand(batch["id"], held)
        except ModelError as error:
            if error.later:
                # The record went with the try: the step is put again.
                return Followed("failed", error.detail)
            return Followed("failed", error.detail)
        return Followed("waiting", f"The batch was handed to {held.provider.name}.")

    # --- stopping --------------------------------------------------------------------------

    async def _stop(self, batch: dict[str, Any], held: _Held, why: str) -> dict[str, Any]:
        if batch["remote_id"]:
            try:
                await self._cancel(held, str(batch["remote_id"]))
            except WireError as error:
                LOG.warning("batch_not_stopped", provider=batch["provider"], kind=error.kind)
        await asyncio.to_thread(
            lambda: self._change(batch["id"], state="stopping", stopping=why, checked_at=None)
        )
        return {**batch, "state": "stopping", "stopping": why, "checked_at": None}

    async def stop(self, step_id: UUID, why: str = "stopped") -> None:
        """The step's batch is no longer wanted: the provider is asked to stop it. What
        it has answered by then is paid for and still fetched."""
        batch = await asyncio.to_thread(self._latest, step_id)
        if batch is None or batch["state"] not in ("submitting", "waiting"):
            return
        if batch["state"] == "submitting" or batch["account_id"] is None:
            await asyncio.to_thread(
                lambda: self._change(
                    batch["id"], state="failed", detail="It was stopped before it was handed in."
                )
            )
            return
        try:
            held = await asyncio.to_thread(self.models._one, batch["account_id"])
        except ModelError:
            await asyncio.to_thread(
                lambda: self._change(batch["id"], state="failed", detail="It was stopped.")
            )
            return
        await self._stop(batch, held, why)

    async def sweep(self) -> int:
        """Batches nobody waits for any more are brought to an end: one whose step is no
        longer running is stopped, and one that is stopping is followed until what it
        answered is fetched. Returns how many it looked at."""

        def orphans() -> list[dict[str, Any]]:
            with self.sessions() as session:
                rows = session.execute(
                    select(ModelBatch, SandboxStep.state)
                    .outerjoin(SandboxStep, SandboxStep.id == ModelBatch.step_id)
                    .where(ModelBatch.state.in_(OPEN))
                ).all()
                return [
                    {**_row(batch), "step_state": state}
                    for batch, state in rows
                    if state != "running"
                ]

        found = await asyncio.to_thread(orphans)
        for batch in found:
            if batch["state"] in ("submitting", "waiting") and batch["step_id"] is not None:
                await self.stop(batch["step_id"], "stopped")
            read = await asyncio.to_thread(self._read, batch["id"])
            if read is not None and read[0]["state"] in ("waiting", "stopping"):
                # One that is no step's is left to its time, and stopped when that is over.
                await self._follow(read[0])
        return len(found)

    # --- what the console and the numbers show ---------------------------------------------

    def used(self, step_id: UUID) -> dict[str, Any] | None:
        """What the step's batches used, where they brought an answer."""
        with self.sessions() as session:
            found = session.scalars(
                select(ModelBatchRequest.usage)
                .join(ModelBatch, ModelBatch.id == ModelBatchRequest.batch_id)
                .where(ModelBatch.step_id == step_id, ModelBatchRequest.usage.is_not(None))
            ).all()
        return plus(*[dict(usage) for usage in found if isinstance(usage, dict)])

    def listing(self, limit: int = 60) -> dict[str, Any]:
        return listing(self.sessions, limit)

    def numbers(self) -> dict[str, Any]:
        """How many batches are out, and for how long the oldest has been."""
        with self.sessions() as session:
            count, oldest = session.execute(
                select(func.count(), func.min(ModelBatch.created_at)).where(
                    ModelBatch.state.in_(OPEN)
                )
            ).one()
        age = (self._now() - _aware(oldest)).total_seconds() if oldest is not None else 0.0
        return {"out": int(count or 0), "oldest_seconds": max(0.0, age)}


def listing(sessions: sessionmaker[Session], limit: int = 60) -> dict[str, Any]:
    """The batches that are out, and the ones that ended last."""
    with sessions() as session:
        rows = session.execute(
            select(ModelBatch, ProviderAccount.name, SandboxStep.name, SandboxRun.title)
            .outerjoin(ProviderAccount, ProviderAccount.id == ModelBatch.account_id)
            .outerjoin(SandboxStep, SandboxStep.id == ModelBatch.step_id)
            .outerjoin(SandboxRun, SandboxRun.id == ModelBatch.run_id)
            .order_by(ModelBatch.created_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        out = session.scalar(
            select(func.count()).select_from(ModelBatch).where(ModelBatch.state.in_(OPEN))
        )
        items = []
        for batch, account, step, title in rows:
            row = _row(batch)
            kind = BY_KEY.get(batch.provider)
            items.append(
                {
                    "id": str(batch.id),
                    "provider": batch.provider,
                    "provider_name": kind.name if kind is not None else batch.provider,
                    "model": batch.model,
                    "account": account,
                    "state": batch.state,
                    "stopping": batch.stopping,
                    "requests": batch.requests,
                    "answered": batch.answered,
                    "detail": batch.detail,
                    "run_id": str(batch.run_id) if batch.run_id else None,
                    "run": title,
                    "step": step,
                    **{
                        name: row[name].isoformat() if row[name] else None
                        for name in ("created_at", "submitted_at", "ended_at", "deadline_at")
                    },
                }
            )
    return {"items": items, "out": int(out or 0)}


COUNTED: Final = ("tokens", "input", "output", "cached", "cache_write", "cost")


def plus(*used: dict[str, Any] | None) -> dict[str, Any] | None:
    """What several questions of one step used, as one: the counts added, and what else
    is said of the last one."""
    given = [entry for entry in used if entry]
    if not given:
        return None
    total: dict[str, Any] = dict(given[-1])
    for name in COUNTED:
        if any(name in entry for entry in given):
            added = sum(entry.get(name) or 0 for entry in given)
            total[name] = round(added, 6) if name == "cost" else int(added)
    if any(entry.get("batch") for entry in given):
        total["batch"] = True
    return total
