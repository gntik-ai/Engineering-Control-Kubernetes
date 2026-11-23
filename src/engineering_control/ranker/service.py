"""The ranking service.

Two small open models that run on the cluster's own processors and are asked by the
console's workers:

    POST /embed    texts            ->  one vector per text: texts that mean alike lie close
    POST /rerank   a text, others   ->  one score per other text: how well it fits the first

Neither decides anything. Vectors find the candidate parents of a planning record among
hundreds; the scores order a short list of them. The models are part of the image, so the
service reaches nothing outside the cluster and no text leaves it, and a fixed model gives
the same answer for the same text every time.

It has no database and no key. It writes nothing and logs no text.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import threading
from collections.abc import AsyncIterator
from typing import Any, Final

import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from engineering_control.config import Settings, get_settings
from engineering_control.logging import configure_logging

LOG = structlog.get_logger(__name__)

# How much one request may carry: enough for a batch, small enough to answer in seconds.
MAX_TEXTS: Final = 64
MAX_CHARACTERS: Final = 12_000
MAX_BODY: Final = 2_000_000


class Models:
    """The two models, loaded once. Loading takes seconds; until then the service says it
    is not ready."""

    def __init__(self, settings: Settings) -> None:
        self.embedding_name = settings.ranker_embedding_model
        self.rerank_name = settings.ranker_rerank_model
        self.directory = settings.ranker_models_dir
        self.threads = settings.ranker_threads
        self.embedding: Any = None
        self.reranker: Any = None
        self.problem: str | None = None
        # One inference at a time: the processors are shared with the rest of the platform.
        self.turn = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.embedding is not None and self.reranker is not None

    def load(self) -> None:
        try:
            # Imported here: only this service needs the models' runtime.
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
            from fastembed.rerank.cross_encoder import (  # type: ignore[import-not-found]
                TextCrossEncoder,
            )

            wanted: dict[str, Any] = {
                "cache_dir": self.directory,
                "threads": self.threads,
                # The models are in the image. Nothing is fetched at run time.
                "local_files_only": True,
            }
            self.embedding = TextEmbedding(self.embedding_name, **wanted)
            self.reranker = TextCrossEncoder(self.rerank_name, **wanted)
            LOG.info("ranker_ready", embedding=self.embedding_name, rerank=self.rerank_name)
        except Exception as error:
            self.problem = f"{type(error).__name__}: {str(error)[:300]}"
            LOG.error("ranker_failed_to_load", problem=self.problem)

    def embed(self, texts: list[str]) -> list[bytes]:
        import numpy

        with self.turn:
            vectors = list(self.embedding.embed(texts, batch_size=16))
        found = []
        for vector in vectors:
            row = numpy.asarray(vector, dtype=numpy.float32)
            length = float(numpy.linalg.norm(row))
            found.append((row / length if length else row).tobytes())
        return found

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        with self.turn:
            return [float(score) for score in self.reranker.rerank(query, documents, batch_size=16)]


def _texts(value: Any, *, least: int = 1) -> list[str] | None:
    if (
        not isinstance(value, list)
        or not least <= len(value) <= MAX_TEXTS
        or not all(isinstance(text, str) for text in value)
    ):
        return None
    # A text that is too long is cut, not refused: the models read its beginning anyway.
    return [text[:MAX_CHARACTERS] or " " for text in value]


def create_app(settings: Settings | None = None, models: Any = None) -> Starlette:
    resolved = settings or get_settings()
    held = models or Models(resolved)

    async def body_of(request: Request) -> dict[str, Any] | None:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return None
        try:
            value = await request.json()
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    async def healthz(_request: Request) -> JSONResponse:
        if held.problem:
            return JSONResponse({"status": "failed", "detail": held.problem}, status_code=503)
        return JSONResponse(
            {
                "status": "ok" if held.ready else "loading",
                "ready": held.ready,
                "embedding": held.embedding_name,
                "rerank": held.rerank_name,
            },
            status_code=200 if held.ready else 503,
        )

    async def embed(request: Request) -> JSONResponse:
        body = await body_of(request)
        texts = _texts((body or {}).get("texts"))
        if texts is None:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        if not held.ready:
            return JSONResponse({"detail": "the models are loading"}, status_code=503)
        vectors = await asyncio.to_thread(held.embed, texts)
        return JSONResponse(
            {
                "model": held.embedding_name,
                "dim": len(vectors[0]) // 4,
                "vectors": [base64.b64encode(vector).decode() for vector in vectors],
            }
        )

    async def rerank(request: Request) -> JSONResponse:
        body = await body_of(request) or {}
        query = body.get("query")
        documents = _texts(body.get("documents"))
        if not isinstance(query, str) or not query.strip() or documents is None:
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        if not held.ready:
            return JSONResponse({"detail": "the models are loading"}, status_code=503)
        scores = await asyncio.to_thread(held.rerank, query[:MAX_CHARACTERS], documents)
        return JSONResponse({"model": held.rerank_name, "scores": scores})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if models is None:
            # In the background: the service answers that it is loading meanwhile.
            threading.Thread(target=held.load, name="ranker-models", daemon=True).start()
        yield

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/embed", embed, methods=["POST"]),
            Route("/rerank", rerank, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.ranker_host,
        port=settings.ranker_port,
        log_config=None,
        access_log=False,
    )
