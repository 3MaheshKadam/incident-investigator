"""Opt-in investigator tools backed by retrieval: `find_evidence` and `suggest_hypotheses`.

Both are read-only. They return text drawn from the incident's own logs, deploys and config changes
(and, for hypotheses, from the runbook and past incidents), so their output is as untrusted as any
other tool output and goes through the same guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from agent.data import ScenarioData
from agent.tools import Toolbox
from retrieval.embed import Embedder
from retrieval.evidence import EvidenceRetriever
from retrieval.kb import KnowledgeBase, signature

FIND_EVIDENCE_SPEC: dict[str, Any] = {
    "name": "find_evidence",
    "description": (
        "Search this incident's logs, deploys and config changes by meaning and keywords and get the "
        "best matches, ranked, from all three sources. Identical log lines are grouped with a count and "
        "time range. Use it when you do not know the exact words to grep for."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you are looking for, in plain words"},
            "limit": {"type": "integer", "description": "Max items (default 8, up to 15)"},
        },
        "required": ["query"],
    },
}
SUGGEST_SPEC: dict[str, Any] = {
    "name": "suggest_hypotheses",
    "description": (
        "Get candidate root cause categories ranked by how well this incident matches a runbook, the "
        "most relevant evidence, and similar past incidents. These are leads to check, not a verdict: "
        "confirm with evidence before reporting."
    ),
    "parameters": {"type": "object", "properties": {}},
}


class RetrievalToolbox(Toolbox):
    extra_specs: ClassVar[list[dict[str, Any]]] = [FIND_EVIDENCE_SPEC, SUGGEST_SPEC]

    def __init__(self, data: ScenarioData, data_dir: Path, history_ids: list[str] | None = None,
                 embedder: Embedder | None = None, base: Toolbox | None = None):
        super().__init__(data)
        self.base = base
        self.retriever = EvidenceRetriever(data, embedder)
        if history_ids is None:  # past incidents are the dev split, never this incident itself
            manifest = json.loads((data_dir / "manifest.json").read_text())["scenarios"]
            history_ids = [e["id"] for e in manifest if e["split"] == "dev"]
        self.kb = KnowledgeBase.build(data_dir, [i for i in history_ids if i != data.id], embedder=embedder)
        self.sig = signature(data, self.retriever)

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name == "find_evidence":
            return self.find_evidence(str(args.get("query") or ""), args.get("limit", 8))
        if name == "suggest_hypotheses":
            return self.suggest_hypotheses()
        return (self.base or super()).call(name, args)

    def find_evidence(self, query: str, limit: Any = 8) -> str:
        if not query.strip():
            return "error: find_evidence needs a non-empty query"
        n = max(1, min(int(limit), 15))
        docs = self.retriever.search(query, "rrf", k=n)
        if not docs:
            return "no matches"
        return "\n".join([f"{len(docs)} best matches, most relevant first"] + [f"- [{d.kind}] {d.text}" for d in docs])

    def suggest_hypotheses(self) -> str:
        runbook = self.kb.rank(self.sig, kinds=("runbook",))[:3]
        past = self.kb.rank(self.sig, kinds=("history",), top=3)[:2]
        slate = self.retriever.slate(self.data.alert, "bm25", alert=self.data.alert)
        lines = ["leads, best first (verify before reporting):"]
        lines += [f"{i}. {h.category} (runbook match)" for i, h in enumerate(runbook, 1)]
        lines.append("most relevant evidence:")
        lines += [f"- [{d.kind}] {d.text}" for d in slate]
        if past:
            lines.append("similar past incidents (the cause may differ):")
            lines += [f"- {h.support[0]}: {h.category}" for h in past if h.support]
        return "\n".join(lines)
