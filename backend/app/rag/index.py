"""The Chroma index and its embedding functions.

Sarvam has no embeddings endpoint, so embeddings are local:

* ``default``: Chroma's built-in all-MiniLM-L6-v2 (ONNX). Downloaded once on
  first use, then offline. Questions are translated to English before they
  are embedded, which is what this model reads best.
* ``hashing``: bag of words and word pairs hashed into a fixed vector. No
  download, no model, fully deterministic. Weaker on paraphrase; used by the
  tests and as an offline fallback.
"""

from __future__ import annotations

import re
import zlib
from functools import lru_cache
from pathlib import Path

import chromadb
import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings

from app import config

COLLECTION = "praman_corpus"

_WORD = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have i in is it its my of on or the this to was what when "
    "which who will with does do can how much many me your you our".split()
)


class HashingEmbedding(EmbeddingFunction):
    """Offline lexical embeddings: hashed unigrams and bigrams, L2-normalised."""

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def __call__(self, input: Documents) -> Embeddings:
        return [self._embed(text) for text in input]

    def _embed(self, text: str) -> np.ndarray:
        words = [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]
        vector = np.zeros(self.dim, dtype=np.float32)
        vector[0] = 1e-3  # never a zero vector, so cosine distance is always defined
        for token in words + [f"{a} {b}" for a, b in zip(words, words[1:])]:
            vector[1 + zlib.crc32(token.encode("utf-8")) % (self.dim - 1)] += 1.0
        return vector / np.linalg.norm(vector)

    @staticmethod
    def name() -> str:
        return "praman-hashing"

    def get_config(self) -> dict:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict) -> "HashingEmbedding":
        return HashingEmbedding(**config)


def embedding_function(kind: str | None = None) -> EmbeddingFunction:
    kind = (kind or config.RAG_EMBEDDINGS).lower()
    if kind == "hashing":
        return HashingEmbedding()
    if kind == "default":
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        return DefaultEmbeddingFunction()
    raise ValueError(f"unknown RAG_EMBEDDINGS {kind!r}; use default or hashing")


def open_collection(path: Path | str, embedding: EmbeddingFunction):
    """Open (creating if needed) the persistent collection at ``path``. No telemetry.

    One collection per embedding kind, so an index built with one model is never
    queried with another.
    """
    client = chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))
    return client.get_or_create_collection(
        f"{COLLECTION}-{embedding.name()}", embedding_function=embedding, metadata={"hnsw:space": "cosine"}
    )


@lru_cache(maxsize=1)
def default_collection():
    """The app's index at backend/data/index with the configured embeddings."""
    return open_collection(config.RAG_INDEX_DIR, embedding_function())
