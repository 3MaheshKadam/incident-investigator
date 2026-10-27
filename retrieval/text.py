"""Tokenising for log-like text.

Identifiers matter in incident evidence (v1.6.0, new_ranking_model, NullPointerException), so a token
is kept whole and also split into its parts: "NullPointerException" gives the whole word and
null / pointer / exception.
"""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "were", "with"]
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def stem(tok: str) -> str:
    """Very light suffix stripping, enough to match restarts / restarted / restarting."""
    for suffix in ("ing", "ed", "es", "s"):
        if tok.endswith(suffix) and len(tok) - len(suffix) >= 4:
            return tok[: -len(suffix)]
    return tok


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        whole = word.strip(".-_").lower()
        if not whole:
            continue
        if whole not in STOPWORDS:
            out.append(stem(whole))
        parts = [p.lower() for piece in re.split(r"[_.\-]", word) for p in _CAMEL.findall(piece)]
        parts = [p for p in parts if len(p) > 1 and not p.isdigit() and p not in STOPWORDS]
        if len(parts) > 1 or (parts and parts[0] != whole):
            out.extend(stem(p) for p in parts)
    return out
