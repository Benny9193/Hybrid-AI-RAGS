"""Pluggable embedders.

``hashing`` is the default: deterministic, offline, zero extra dependencies.
For ~900 schema objects whose text is dominated by identifiers it works
surprisingly well, and the BM25 half of hybrid retrieval covers exact terms.
Swap in ``fastembed`` (local ONNX model) or ``voyage`` (API) for better
semantic matching of paraphrased questions.
"""

from __future__ import annotations

import hashlib
import os
from typing import Protocol

import numpy as np

from .text import tokens


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


class HashingEmbedder:
    """Signed feature hashing over word unigrams, bigrams and char trigrams."""

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _features(self, text: str) -> list[tuple[str, float]]:
        words = tokens(text)
        feats: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        feats += [(f"b:{a}_{b}", 0.5) for a, b in zip(words, words[1:])]
        for w in set(words):
            padded = f"#{w}#"
            feats += [(f"c:{padded[i:i + 3]}", 0.2) for i in range(len(padded) - 2)]
        return feats

    def _embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for feat, weight in self._features(text):
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            sign = 1.0 if (h >> 63) & 1 else -1.0
            v[h % self.dim] += sign * weight
        # Sublinear TF so a 200-column table doesn't drown out its description.
        v = np.sign(v) * np.log1p(np.abs(v))
        return v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.stack([self._embed(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(self._embed(text)[None, :])[0]


class FastEmbedEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding  # optional dependency

        self._model = TextEmbedding(model_name=model)
        self.name = f"fastembed-{model}"

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return _normalize(np.array(list(self._model.embed(texts)), dtype=np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(np.array(list(self._model.query_embed([text])), dtype=np.float32))[0]


class VoyageEmbedder:
    def __init__(self, model: str = "voyage-3.5-lite"):
        import voyageai  # optional dependency; reads VOYAGE_API_KEY

        self._client = voyageai.Client()
        self._model = model
        self.name = f"voyage-{model}"

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            out += self._client.embed(texts[i:i + 128], model=self._model, input_type="document").embeddings
        return _normalize(np.array(out, dtype=np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        e = self._client.embed([text], model=self._model, input_type="query").embeddings
        return _normalize(np.array(e, dtype=np.float32))[0]


def get_embedder(spec: str | None = None) -> Embedder:
    """``hashing`` | ``hashing:2048`` | ``fastembed[:model]`` | ``voyage[:model]``."""
    spec = spec or os.environ.get("EDS_RAG_EMBEDDER", "hashing")
    kind, _, arg = spec.partition(":")
    if kind == "hashing":
        return HashingEmbedder(int(arg) if arg else 1024)
    if kind == "fastembed":
        return FastEmbedEmbedder(arg) if arg else FastEmbedEmbedder()
    if kind == "voyage":
        return VoyageEmbedder(arg) if arg else VoyageEmbedder()
    raise ValueError(f"unknown embedder {spec!r}")
