"""Local embeddings. No network calls at embed time, no API keys, no cost.

``Embedder`` is a tiny protocol so the pipeline and the tests don't depend on torch:
tests use a deterministic fake (tests/fakes.py; instant, never downloads a model); production uses
``SentenceTransformerEmbedder`` (default ``BAAI/bge-small-en-v1.5``, 384 dims).

Notes for Phase 4 (retrieval): bge models expect the query -- NOT the passages -- to be
prefixed with ``Represent this sentence for searching relevant passages: ``. Passages are
embedded as-is, which is what ``embed_passages`` does. Vectors are L2-normalised, so cosine
distance (the HNSW index's operator class) is the right metric.
"""

import asyncio
from typing import Protocol

from app.config import get_settings


class Embedder(Protocol):
    dim: int
    model_name: str

    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Lazy: importing this module does not import torch; the model loads on first use."""

    def __init__(self, model_name: str | None = None, dim: int | None = None) -> None:
        s = get_settings()
        self.model_name = model_name or s.embedding_model
        self.dim = dim or s.embedding_dim
        self._batch = s.kb_embed_batch_size
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        from sentence_transformers import SentenceTransformer

        # trust_remote_code stays False: never execute code shipped with a model repo.
        model = SentenceTransformer(self.model_name, device="cpu", trust_remote_code=False)
        got = model.get_sentence_embedding_dimension()
        if got != self.dim:
            raise RuntimeError(
                f"embedding model {self.model_name} has {got} dims but EMBEDDING_DIM={self.dim}"
            )
        return model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            self._model = self._load()
        vecs = self._model.encode(
            texts, batch_size=self._batch, normalize_embeddings=True, show_progress_bar=False
        )
        return [[float(x) for x in v] for v in vecs]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._lock:  # one torch call at a time; keeps CPU/RAM bounded
            return await asyncio.to_thread(self._encode, texts)
