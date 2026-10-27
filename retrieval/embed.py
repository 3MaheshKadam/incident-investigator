"""Embedders: turn text into vectors and compare them with cosine similarity.

NgramEmbedder needs nothing and works offline (and in CI). It matches on shared character
fragments, so it tolerates spelling and wording variants, but it does not know meaning: it is a
fuzzy lexical matcher, not a neural model. OllamaEmbedder asks a local model for real embeddings.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from functools import lru_cache
from typing import Protocol

import httpx

Vector = dict[int, float] | list[float]


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[Vector]: ...


def cosine(a: Vector, b: Vector) -> float:
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) > len(b):
            a, b = b, a
        dot = sum(v * b.get(k, 0.0) for k, v in a.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b, strict=True))  # type: ignore[arg-type]
        na = math.sqrt(sum(x * x for x in a))  # type: ignore[union-attr]
        nb = math.sqrt(sum(y * y for y in b))  # type: ignore[union-attr]
    return dot / (na * nb) if na and nb else 0.0


class NgramEmbedder:
    name = "ngram"

    def __init__(self, sizes: tuple[int, ...] = (3, 4, 5), buckets: int = 1 << 18):
        self.sizes, self.buckets = sizes, buckets

    def embed(self, texts: list[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for text in texts:
            norm = re.sub(r"\d+", "0", text.lower())
            norm = re.sub(r"[^a-z0-9_]+", " ", norm)
            counts: Counter[int] = Counter()
            for word in norm.split():
                padded = f" {word} "
                for n in self.sizes:
                    for i in range(len(padded) - n + 1):
                        counts[hash_str(padded[i : i + n]) % self.buckets] += 1
            vectors.append({k: 1 + math.log(v) for k, v in counts.items()})
        return vectors


def hash_str(s: str) -> int:
    """A stable hash (Python's own str hash changes between runs)."""
    h = 2166136261
    for ch in s.encode():
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h


class EmbedderUnavailable(RuntimeError):
    """The embedding service cannot be used; the message says what to do."""


class OllamaEmbedder:
    def __init__(self, model: str = "nomic-embed-text", host: str | None = None,
                 client: httpx.Client | None = None):
        self.name = f"ollama:{model}"
        # The same OLLAMA_BASE_URL the chat model uses, so one setting points both at the same machine.
        host = host or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
        self.model, self.host = model, host.rstrip("/")
        self.client = client or httpx.Client(timeout=120)
        self._cache: dict[str, Vector] = {}  # the same incident text is embedded many times per run

    def embed(self, texts: list[str]) -> list[Vector]:
        missing = list(dict.fromkeys(t for t in texts if t not in self._cache))
        for t, v in zip(missing, self._embed_remote(missing), strict=True):
            self._cache[t] = v
        return [self._cache[t] for t in texts]

    def _embed_remote(self, texts: list[str]) -> list[Vector]:
        out: list[Vector] = []
        for i in range(0, len(texts), 64):
            try:
                r = self.client.post(f"{self.host}/api/embed", json={"model": self.model,
                                                                      "input": texts[i : i + 64]})
            except httpx.TransportError as e:
                raise EmbedderUnavailable(
                    f"cannot reach Ollama at {self.host} ({type(e).__name__}). Start it, or set "
                    "OLLAMA_BASE_URL (in .env or the shell) to the machine that runs it.") from e
            if r.status_code == 404:
                raise EmbedderUnavailable(
                    f"Ollama at {self.host} does not have the model {self.model!r}. "
                    f"Run: ollama pull {self.model}")
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
        return out


def make_embedder(spec: str) -> Embedder:
    if spec == "ngram":
        return NgramEmbedder()
    if spec.startswith("ollama"):
        return OllamaEmbedder(spec.partition(":")[2] or "nomic-embed-text")
    raise ValueError(f"unknown embedder {spec!r}; use 'ngram' or 'ollama[:model]'")


@lru_cache(maxsize=4)
def shared_embedder(spec: str | None = None) -> Embedder:
    """One embedder per process for a spec (default: RETRIEVAL_EMBEDDER, else 'ngram'), so its cache is shared."""
    return make_embedder(spec or os.environ.get("RETRIEVAL_EMBEDDER") or "ngram")
