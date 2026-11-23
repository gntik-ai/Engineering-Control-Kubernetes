"""Fetch the ranking service's two models into a folder. Run once, when the image is built:

    python -m engineering_control.ranker.fetch /opt/models

The service itself never fetches anything: it runs where nothing outside the cluster can
be reached, and finds the models in that folder.
"""

from __future__ import annotations

import sys

from engineering_control.config import Settings


def fetch(directory: str, embedding: str, rerank: str) -> None:
    from fastembed import TextEmbedding  # type: ignore[import-not-found]
    from fastembed.rerank.cross_encoder import (  # type: ignore[import-not-found]
        TextCrossEncoder,
    )

    # Loading a model fetches it; asking it once proves that what was fetched works.
    vector = next(iter(TextEmbedding(embedding, cache_dir=directory).embed(["a text"])))
    scores = list(TextCrossEncoder(rerank, cache_dir=directory).rerank("a text", ["another"]))
    print(f"{embedding}: {len(vector)} numbers a text; {rerank}: {len(scores)} score a pair")


def main() -> None:
    fields = Settings.model_fields
    fetch(
        sys.argv[1] if len(sys.argv) > 1 else str(fields["ranker_models_dir"].default),
        str(fields["ranker_embedding_model"].default),
        str(fields["ranker_rerank_model"].default),
    )


if __name__ == "__main__":
    main()
