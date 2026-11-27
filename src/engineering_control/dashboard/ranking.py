"""Asking the ranking service, and keeping what it answers.

The ranking service (``engineering_control.ranker``) runs two open models in the cluster.
This is its client, and the store of what it said: one vector for each planning record's
text, and one score for each pair of records it was asked about. Both are kept until the
text or the model changes, so nothing is asked twice.

Neither a vector nor a score decides anything. They are two of the signals that the
measurement on Reviews → Reorganize compares with word matching.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable
from typing import Any, Final
from uuid import UUID

import httpx
import numpy
from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.dashboard.review_graph import ID, PARENT
from engineering_control.db.models import PairScore, RecordVector

# How much of a record's text is sent: the models read its beginning.
TEXT: Final = 2400
# How many texts go in one request, and how long one may take on a busy processor.
BATCH: Final = 16
SECONDS: Final = 180.0


class RankerError(RuntimeError):
    """The ranking service did not answer, or not with what was asked for."""


def text_of(title: str, body: str | None) -> str:
    """A record's text as the models are given it. A line that names the record's parent,
    and identifiers, are taken out: what is measured is whether the parent is found from
    what the record says, not from where it says it belongs."""
    cleaned = ID.sub(" ", PARENT.sub("", body or ""))
    return (title.strip() + "\n" + " ".join(cleaned.split()))[:TEXT]


def digest_of(*texts: str) -> str:
    return hashlib.sha256("\x1f".join(texts).encode()).hexdigest()


class Ranker:
    """The ranking service, as the console's workers ask it."""

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=SECONDS)

    async def _ask(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            answer = await self.client.post(self.url + path, json=body)
        except httpx.HTTPError as error:
            raise RankerError("The ranking service did not answer.") from error
        if answer.status_code != 200:
            raise RankerError(f"The ranking service answered {answer.status_code}.")
        try:
            value = answer.json()
        except ValueError as error:
            raise RankerError("The ranking service answered with no numbers.") from error
        if not isinstance(value, dict):
            raise RankerError("The ranking service answered with no numbers.")
        return value

    async def health(self) -> dict[str, Any]:
        """Whether the service is there with its models loaded, and which they are."""
        try:
            answer = await self.client.get(self.url + "/healthz", timeout=10)
            value = answer.json()
        except (httpx.HTTPError, ValueError):
            return {"ready": False, "detail": "The ranking service does not answer."}
        if not isinstance(value, dict):
            return {"ready": False, "detail": "The ranking service does not answer."}
        return {"ready": bool(value.get("ready")), **value}

    async def embed(self, texts: list[str]) -> tuple[str, list[bytes]]:
        value = await self._ask("/embed", {"texts": texts})
        try:
            vectors = [base64.b64decode(vector) for vector in value["vectors"]]
            model = str(value["model"])
        except (KeyError, TypeError, ValueError) as error:
            raise RankerError("The ranking service answered with no vectors.") from error
        if len(vectors) != len(texts) or not all(vectors) or len({len(v) for v in vectors}) != 1:
            raise RankerError("The ranking service answered with the wrong vectors.")
        return model, vectors

    async def rerank(self, query: str, documents: list[str]) -> tuple[str, list[float]]:
        value = await self._ask("/rerank", {"query": query, "documents": documents})
        try:
            scores = [float(score) for score in value["scores"]]
            model = str(value["model"])
        except (KeyError, TypeError, ValueError) as error:
            raise RankerError("The ranking service answered with no scores.") from error
        if len(scores) != len(documents):
            raise RankerError("The ranking service answered with the wrong scores.")
        return model, scores


def reader(url: str) -> Callable[[list[str]], tuple[str, list[bytes]]]:
    """A way to have texts read by the ranking service from a worker thread, which has
    no event loop of its own: each call opens its own connection and closes it."""

    async def read(texts: list[str]) -> tuple[str, list[bytes]]:
        async with httpx.AsyncClient(timeout=SECONDS) as client:
            return await Ranker(url, client).embed(texts)

    return lambda texts: asyncio.run(read(texts))


def stored_model(db: Session, table: Any, project: UUID) -> str | None:
    """The model whose answers were kept last for the project."""
    return db.scalar(
        select(table.model)
        .where(table.project_id == project)
        .order_by(table.updated_at.desc(), table.id)
        .limit(1)
    )


def vectors(db: Session, project: UUID, model: str) -> dict[tuple[str, UUID], Any]:
    """Every vector kept for the project's records, by kind and record."""
    return {
        (kind, record): numpy.frombuffer(blob, dtype=numpy.float32)
        for kind, record, blob in db.execute(
            select(RecordVector.kind, RecordVector.record_id, RecordVector.vector).where(
                RecordVector.project_id == project, RecordVector.model == model
            )
        )
    }


def vector_digests(db: Session, project: UUID, model: str) -> dict[tuple[str, UUID], str]:
    return {
        (kind, record): digest
        for kind, record, digest in db.execute(
            select(RecordVector.kind, RecordVector.record_id, RecordVector.digest).where(
                RecordVector.project_id == project, RecordVector.model == model
            )
        )
    }


def keep_vector(
    db: Session, project: UUID, kind: str, record: UUID, model: str, digest: str, vector: bytes
) -> None:
    row = db.scalar(
        select(RecordVector).where(
            RecordVector.project_id == project,
            RecordVector.kind == kind,
            RecordVector.record_id == record,
            RecordVector.model == model,
        )
    )
    if row is None:
        row = RecordVector(project_id=project, kind=kind, record_id=record, model=model)
        db.add(row)
    row.digest, row.vector = digest, vector


def scores(
    db: Session, project: UUID, model: str
) -> dict[tuple[str, UUID, UUID], tuple[str, float]]:
    """Every score kept for the project's pairs: by kind, child and parent, with the
    digest of the texts it was made from."""
    return {
        (kind, child, parent): (digest, score)
        for kind, child, parent, digest, score in db.execute(
            select(
                PairScore.kind,
                PairScore.child_id,
                PairScore.parent_id,
                PairScore.digest,
                PairScore.score,
            ).where(PairScore.project_id == project, PairScore.model == model)
        )
    }


def keep_score(
    db: Session,
    project: UUID,
    kind: str,
    child: UUID,
    parent: UUID,
    model: str,
    digest: str,
    score: float,
) -> None:
    row = db.scalar(
        select(PairScore).where(
            PairScore.project_id == project,
            PairScore.kind == kind,
            PairScore.child_id == child,
            PairScore.parent_id == parent,
            PairScore.model == model,
        )
    )
    if row is None:
        row = PairScore(
            project_id=project, kind=kind, child_id=child, parent_id=parent, model=model
        )
        db.add(row)
    row.digest, row.score = digest, score
