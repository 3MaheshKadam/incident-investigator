"""Rank the evidence of one incident against a query (usually the alert).

Methods:
  bm25    keyword match
  ngram   character n-gram cosine (or any Embedder)
  rrf     bm25 and ngram fused with reciprocal rank fusion
  rrf+    rrf plus a third ranking from structured signals (below)

The structured signal is a ranking of documents by facts the alert gives us for free: they belong to
the alerting service, they are errors or warnings, a deploy or config change landed shortly before the
alert, or a log message first appeared right around the alert's start (a new kind of message at the
moment things broke, on any service). It uses nothing but the alert and the timestamps.

`slate()` returns a fixed mix (top logs, plus the best deploy and config change) because root-cause
evidence lives in different sources and one mixed ranking lets the noisiest source crowd out the rest.
"""

from __future__ import annotations

from datetime import datetime

from agent.data import ScenarioData
from retrieval.bm25 import BM25
from retrieval.docs import SEVERITY, Doc, build_docs, parse_alert
from retrieval.embed import Embedder, NgramEmbedder, cosine
from retrieval.fusion import rank_order, rrf

METHODS = ["bm25", "ngram", "rrf", "rrf+"]


def _minutes_before(ts: str, alert_ts: str) -> float | None:
    try:
        a = datetime.fromisoformat(alert_ts)
        t = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (a - t).total_seconds() / 60


class EvidenceRetriever:
    def __init__(self, data: ScenarioData, embedder: Embedder | None = None):
        self.data = data
        self.docs: list[Doc] = build_docs(data)
        self.embedder = embedder or NgramEmbedder()
        texts = [d.text for d in self.docs]
        self._bm25 = BM25(texts)
        self._vecs = self.embedder.embed(texts)

    def signal_scores(self, alert: str) -> list[float]:
        service, start = parse_alert(alert)
        out = []
        for d in self.docs:
            s = 0.0
            if service and d.service == service:
                s += 1.0
            s += SEVERITY.get(d.level, 0) * 0.5
            if d.kind in ("deploy", "config") and start:
                gap = _minutes_before(d.last_ts, start)
                if gap is not None and 0 <= gap <= 60:
                    s += 1.5 * (1 - gap / 60)
            elif d.kind == "log" and d.level and start:
                gap = _minutes_before(d.first_ts, start)  # positive: it began before the alert
                if gap is not None and -5 <= gap <= 20:
                    s += 2.0
            out.append(s if (d.level or d.kind != "log") else 0.0)
        return out

    def search(self, query: str, method: str = "rrf+", k: int = 8, alert: str | None = None) -> list[Doc]:
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        bm = rank_order(self._bm25.scores(query))
        qv = self.embedder.embed([query])[0]
        dn = rank_order([cosine(qv, v) for v in self._vecs])
        if method == "bm25":
            order = bm
        elif method == "ngram":
            order = dn
        else:
            lists = [bm, dn]
            if method == "rrf+":
                sig = self.signal_scores(alert if alert is not None else query)
                lists.append(sorted(range(len(sig)), key=lambda i: -sig[i]))
            fused = rrf(lists)
            order = sorted(fused, key=lambda i: -fused[i])
        return [self.docs[i] for i in order[:k]]

    def slate(self, query: str, method: str = "rrf+", logs: int = 4, deploys: int = 1,
              configs: int = 1, alert: str | None = None) -> list[Doc]:
        """The best few logs, then the deploy and config change that best fit the alert's timing.

        Logs are ranked by the chosen method. Deploys and config changes are ranked by the structured
        signal, because the alert text says nothing about them but their timing says a lot.
        """
        out = [d for d in self.search(query, method, k=len(self.docs), alert=alert) if d.kind == "log"][:logs]
        sig = self.signal_scores(alert if alert is not None else query)
        for kind, n in (("deploy", deploys), ("config", configs)):
            ranked = sorted((i for i, d in enumerate(self.docs) if d.kind == kind), key=lambda i: -sig[i])
            out.extend(self.docs[i] for i in ranked[:n])
        return out
