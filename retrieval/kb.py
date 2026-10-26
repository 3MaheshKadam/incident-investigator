"""A knowledge base of past incidents and runbook entries, and ranked root-cause hypotheses from it.

Each entry has a text signature and a label (the root cause category it was resolved as). A new
incident is turned into the same kind of signature, the entries are ranked with BM25 and n-gram
cosine fused by RRF, and each category is scored by its single best entry.

Two kinds of entry:
  history  past incidents, signature built from the alert and its best evidence, labelled with how
           it was resolved. Leakage rule: an incident never retrieves itself, and a held-out
           incident only ever sees history from the dev split (see `retrieval.eval`).
  runbook  a short written description of what each category looks like. It works with no history.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.data import ScenarioData
from agent.tools import CATEGORIES
from retrieval.bm25 import BM25
from retrieval.embed import Embedder, NgramEmbedder, cosine
from retrieval.evidence import EvidenceRetriever
from retrieval.fusion import rank_order, rrf

ONSET_SCORE = 2.0  # a log message that first appeared at the alert, or a change shortly before it

RUNBOOK = {
    "bad_deploy": "errors begin right after a deploy of the failing service; exceptions such as "
                  "NullPointerException or a stack trace in the new version; roll back the release",
    "memory_leak": "memory grows steadily until the container is OOMKilled and restarted; warnings "
                   "about heap usage near the limit; pod restarts repeat; restart the service",
    "expired_certificate": "TLS handshake failed; x509 certificate has expired or is not valid; "
                           "connection failures to a partner or payments endpoint; renew the certificate",
    "slow_query": "a database query is slow, a full table scan (Seq Scan) with no index; latency is "
                  "high while the database and service stay up; add an index",
    "feature_flag": "a feature flag or config change was switched on, then an experimental code path "
                    "times out or errors; turn the flag off",
    "dependency_outage": "an upstream or downstream service is down: connection refused, failing "
                         "health checks, 5xx from the gateway; restore or fail over the dependency",
}


def mask_signature(text: str) -> str:
    text = re.sub(r"v?\d+(\.\d+)+", " ", text)  # versions, addresses
    return re.sub(r"\d+", " ", text)


def signature(data: ScenarioData, retriever: EvidenceRetriever | None = None) -> str:
    """What changed around the alert: the alert's wording, new kinds of log message near its start,
    and any deploy or config change just before it. Steady background messages are left out, because
    every incident has them and they would make all incidents look alike."""
    r = retriever or EvidenceRetriever(data)
    sig = r.signal_scores(data.alert)
    changed = [d for d, s in zip(r.docs, sig, strict=True) if s >= ONSET_SCORE and (d.level or d.kind != "log")]
    alert = re.sub(r"ALERT \[\w+\] [\w\-]+:", "", data.alert.split(" since ")[0])
    return mask_signature(" \n".join([alert, *(d.text for d in changed)]))


@dataclass
class Entry:
    id: str
    label: str
    kind: str  # history | runbook
    text: str


@dataclass
class Hypothesis:
    category: str
    score: float
    support: list[str] = field(default_factory=list)  # ids of the entries that voted for it


class KnowledgeBase:
    def __init__(self, entries: list[Entry], embedder: Embedder | None = None):
        self.entries = entries
        self.embedder = embedder or NgramEmbedder()
        texts = [e.text for e in entries]
        self._bm25 = BM25(texts)
        self._vecs = self.embedder.embed(texts)

    @classmethod
    def build(cls, data_dir: Path, history_ids: list[str], runbook: bool = True,
              embedder: Embedder | None = None) -> KnowledgeBase:
        entries: list[Entry] = []
        if runbook:
            entries += [Entry(f"runbook:{c}", c, "runbook", t) for c, t in RUNBOOK.items()]
        for sid in history_ids:
            label = json.loads((data_dir / sid / "answer.json").read_text())["root_cause_category"]
            entries.append(Entry(sid, label, "history", signature(ScenarioData(data_dir / sid))))
        return cls(entries, embedder)

    def rank(self, text: str, exclude: str | None = None, top: int = 5,
             kinds: tuple[str, ...] = ()) -> list[Hypothesis]:
        """Categories best first. `kinds` limits which entries may vote (default: all)."""
        qv = self.embedder.embed([text])[0]
        bm = self._bm25.scores(text)
        dn = [cosine(qv, v) for v in self._vecs]
        for i, e in enumerate(self.entries):
            if e.id == exclude or (kinds and e.kind not in kinds):
                bm[i] = dn[i] = 0.0
        order = rrf([rank_order(bm), rank_order(dn)])
        best = sorted(order, key=lambda i: -order[i])[:top]
        votes: dict[str, Hypothesis] = {}
        for i in best:
            e = self.entries[i]
            h = votes.setdefault(e.label, Hypothesis(e.label, 0.0))
            h.score = max(h.score, order[i])  # best entry, not a sum: several weak matches must not
            h.support.append(e.id)            # outvote one strong match
        ranked = sorted(votes.values(), key=lambda h: -h.score)
        seen = {h.category for h in ranked}
        # Categories with no vote still appear, last, so the list always covers every category.
        return ranked + [Hypothesis(c, 0.0) for c in CATEGORIES if c not in seen]


