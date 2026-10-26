"""BM25 (Okapi), written out so there is no dependency to install."""

from __future__ import annotations

import math
from collections import Counter

from retrieval.text import tokens


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokens(t)) for t in texts]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = set(tokens(query))
        out = []
        for d, length in zip(self.docs, self.lengths, strict=True):
            s = 0.0
            for t in q:
                f = d.get(t)
                if f:
                    norm = f + self.k1 * (1 - self.b + self.b * length / (self.avg or 1.0))
                    s += self.idf[t] * f * (self.k1 + 1) / norm
            out.append(s)
        return out
