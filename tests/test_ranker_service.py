"""The ranking service answers with numbers for the texts it is sent, refuses what is not
a request, and says so while its models are loading."""

import base64
from typing import Any

from starlette.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.ranker.service import MAX_CHARACTERS, MAX_TEXTS, Models, create_app


class Loaded:
    ready = True
    problem: str | None = None
    embedding_name = "an-embedding-model"
    rerank_name = "a-reranker"

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def embed(self, texts: list[str]) -> list[bytes]:
        self.seen.append(texts)
        return [bytes(8) for _ in texts]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.seen.append((query, documents))
        return [float(len(document)) for document in documents]


def test_vectors_and_scores_for_what_is_sent() -> None:
    models = Loaded()
    client = TestClient(create_app(Settings(), models))
    assert client.get("/healthz").json() == {
        "status": "ok",
        "ready": True,
        "embedding": "an-embedding-model",
        "rerank": "a-reranker",
    }
    answer = client.post("/embed", json={"texts": ["one", "two"]}).json()
    assert answer["model"] == "an-embedding-model" and answer["dim"] == 2
    assert [base64.b64decode(vector) for vector in answer["vectors"]] == [bytes(8), bytes(8)]
    scored = client.post("/rerank", json={"query": "q", "documents": ["a", "bbb"]}).json()
    assert scored == {"model": "a-reranker", "scores": [1.0, 3.0]}
    # A text that is too long is cut; an empty one is still a text.
    client.post("/embed", json={"texts": ["x" * (MAX_CHARACTERS + 50), ""]})
    assert [len(text) for text in models.seen[-1]] == [MAX_CHARACTERS, 1]


def test_what_is_no_request_is_refused() -> None:
    client = TestClient(create_app(Settings(), Loaded()))
    for body in (
        {},
        {"texts": []},
        {"texts": "one"},
        {"texts": [1]},
        {"texts": ["x"] * (MAX_TEXTS + 1)},
    ):
        assert client.post("/embed", json=body).status_code == 422
    for body in ({"documents": ["a"]}, {"query": " ", "documents": ["a"]}, {"query": "q"}):
        assert client.post("/rerank", json=body).status_code == 422
    assert client.post("/embed", content=b"not json").status_code == 422
    assert client.get("/embed").status_code == 405


def test_it_says_when_the_models_are_not_there() -> None:
    models = Models(Settings())
    client = TestClient(create_app(Settings(), models))
    # Not loaded yet: the cluster waits before it sends anything.
    assert client.get("/healthz").status_code == 503
    assert client.get("/healthz").json()["status"] == "loading"
    assert client.post("/embed", json={"texts": ["one"]}).status_code == 503
    models.problem = "the models could not be loaded"
    failed = client.get("/healthz")
    assert failed.status_code == 503 and failed.json()["status"] == "failed"
