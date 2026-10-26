"""Reciprocal rank fusion: combine ranked lists without having to compare their scores."""

from __future__ import annotations


def rank_order(scores: list[float]) -> list[int]:
    """Indices best first. Ties keep input order; zero scores are not a match and are dropped."""
    return [i for i in sorted(range(len(scores)), key=lambda i: -scores[i]) if scores[i] > 0]


def rrf(rankings: list[list[int]], k: int = 60, weights: list[float] | None = None) -> dict[int, float]:
    fused: dict[int, float] = {}
    for n, ranking in enumerate(rankings):
        w = weights[n] if weights else 1.0
        for pos, idx in enumerate(ranking):
            fused[idx] = fused.get(idx, 0.0) + w / (k + pos + 1)
    return fused
