"""Measure retrieval without calling a model.

    python -m retrieval.eval --split dev
    python -m retrieval.eval --split heldout --label final        (only for the final numbers)
    python -m retrieval.eval --split dev --embedder ollama:nomic-embed-text

Part 1, evidence: does what is returned contain the evidence that identifies the root cause
(`evidence_keys` in answer.json, which the investigator never sees)?
  coverage   mean fraction of the evidence keys found in the returned text
  all-keys   share of incidents where every key was found
  mrr        reciprocal rank of the first returned item containing a key
Part 2, hypotheses: is the true root cause category near the top of the ranked list?
Part 3, paraphrased queries: the same ranking with symptom queries written without the log's words.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

from agent.data import ScenarioData
from agent.env import load_dotenv
from agent.tools import CATEGORIES
from retrieval.embed import EmbedderUnavailable, make_embedder
from retrieval.evidence import METHODS, EvidenceRetriever
from retrieval.kb import KnowledgeBase, signature

DATA = Path(__file__).resolve().parent.parent / "scenarios" / "data"
K = 5

# Hand-written, one per category: how an engineer might describe the symptom in their own words.
PARAPHRASES = {
    "bad_deploy": ["new release is throwing exceptions", "crashes in code that just shipped"],
    "memory_leak": ["pod keeps getting killed for running out of memory", "heap nearly full then restart"],
    "expired_certificate": ["secure connection to the partner fails, credential lapsed", "tls handshake problem"],
    "slow_query": ["database call takes seconds, scans the whole table", "sql is slow"],
    "feature_flag": ["recently enabled experimental behaviour is timing out", "toggle turned on then failures"],
    "dependency_outage": ["cannot reach the service we depend on", "upstream is unreachable"],
}


def load(split: str, data_dir: Path = DATA) -> list[dict]:
    entries = json.loads((data_dir / "manifest.json").read_text())["scenarios"]
    return [e for e in entries if split == "all" or e["split"] == split]


def answer(sid: str, data_dir: Path = DATA) -> dict:
    return json.loads((data_dir / sid / "answer.json").read_text())


def score_texts(texts: list[str], keys: list[str]) -> tuple[float, bool, float]:
    low = [t.lower() for t in texts]
    found = [any(k.lower() in t for t in low) for k in keys]
    mrr = next((1 / (i + 1) for i, t in enumerate(low) if any(k.lower() in t for k in keys)), 0.0)
    return sum(found) / len(keys), all(found), mrr


def tail_lines(data: ScenarioData, k: int, levels: set[str] | None = None) -> list[str]:
    rows = [r for r in data.logs if not levels or r["level"] in levels]
    return [f"{r['ts']} {r['level']} {r['service']}: {r['msg']}" for r in reversed(rows[-k:])]


def summarize(rows: list[tuple[float, bool, float]]) -> dict:
    return {"coverage": round(mean(r[0] for r in rows), 3), "all_keys": round(mean(r[1] for r in rows), 3),
            "mrr": round(mean(r[2] for r in rows), 3), "n": len(rows)}


def evidence_eval(scen: list[dict], embedder_spec: str, data_dir: Path = DATA) -> dict:
    emb = make_embedder(embedder_spec)
    rows: dict[str, list] = defaultdict(list)
    per_kind: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for e in scen:
        data, keys = ScenarioData(data_dir / e["id"]), answer(e["id"], data_dir)["evidence_keys"]
        r = EvidenceRetriever(data, emb)
        runs = {
            "tail (default tool)": tail_lines(data, K),
            "tail errors+warnings": tail_lines(data, K, {"ERROR", "WARN"}),
        }
        for m in METHODS:
            runs[f"logs-only {m}"] = [d.text for d in r.search(data.alert, m, k=K, alert=data.alert) if d.kind == "log"]
            runs[f"slate {m}"] = [d.text for d in r.slate(data.alert, m, alert=data.alert)]
        for name, texts in runs.items():
            s = score_texts(texts, keys)
            rows[name].append(s)
            per_kind[name][e["kind"]].append(s)
    return {"overall": {n: summarize(v) for n, v in rows.items()},
            "per_kind": {n: {k: summarize(v)["coverage"] for k, v in kinds.items()} for n, kinds in per_kind.items()}}


def paraphrase_eval(scen: list[dict], embedder_spec: str, data_dir: Path = DATA) -> dict:
    emb = make_embedder(embedder_spec)
    rows: dict[str, list] = defaultdict(list)
    for e in scen:
        data, keys = ScenarioData(data_dir / e["id"]), answer(e["id"], data_dir)["evidence_keys"]
        r = EvidenceRetriever(data, emb)
        for q in PARAPHRASES[e["kind"]]:
            for m in ("bm25", "ngram", "rrf"):
                texts = [d.text for d in r.search(q, m, k=K) if True]
                rows[m].append(score_texts(texts, keys))
    return {m: summarize(v) for m, v in rows.items()}


def hypothesis_eval(scen: list[dict], embedder_spec: str, data_dir: Path = DATA, history_dir: Path = DATA) -> dict:
    """History is the dev split of `history_dir`. When the incidents being judged live in that same
    directory an incident is left out of its own history; otherwise nothing is excluded (live
    incident ids repeat the simulated ones, so comparing ids would drop unrelated history)."""
    emb = make_embedder(embedder_spec)
    same = data_dir.resolve() == history_dir.resolve()
    dev_ids = [e["id"] for e in load("dev", history_dir)]
    out: dict[str, dict] = {}
    cache_sig = {e["id"]: signature(ScenarioData(data_dir / e["id"])) for e in scen}
    scenarios = {
        "runbook only": (False, True, False),
        "history only": (True, False, False),
        "history + runbook": (True, True, False),
        "unseen kind, history only": (True, False, True),
        "unseen kind, history + runbook": (True, True, True),
    }
    for name, (hist, book, unseen) in scenarios.items():
        ranks = []
        for e in scen:
            truth = answer(e["id"], data_dir)["root_cause_category"]
            ids = [i for i in dev_ids if not (same and i == e["id"])] if hist else []
            if unseen:
                ids = [i for i in ids if answer(i, history_dir)["root_cause_category"] != truth]
            kb = KnowledgeBase.build(history_dir, ids, runbook=book, embedder=emb)
            ranked = [h.category for h in kb.rank(cache_sig[e["id"]])]
            ranks.append(ranked.index(truth) + 1)
        out[name] = {"top1": round(mean(r == 1 for r in ranks), 3), "top3": round(mean(r <= 3 for r in ranks), 3),
                     "mrr": round(mean(1 / r for r in ranks), 3), "n": len(ranks)}
    out["chance"] = {"top1": round(1 / len(CATEGORIES), 3), "top3": round(3 / len(CATEGORIES), 3)}
    return out


def show(title: str, table: dict, cols: list[str]) -> None:
    print(f"\n{title}")
    width = max(len(n) for n in table) + 2
    print(" " * width + "".join(f"{c:>10}" for c in cols))
    for name, vals in table.items():
        print(f"{name:<{width}}" + "".join(f"{vals.get(c, ''):>10}" for c in cols))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    p.add_argument("--embedder", default="ngram", help="ngram | ollama[:model]")
    p.add_argument("--data", type=Path, default=DATA, help="incidents to judge (default: the simulated set)")
    p.add_argument("--label", default="")
    p.add_argument("--out", type=Path, default=Path("retrieval/results"))
    args = p.parse_args(argv)
    load_dotenv()
    scen = load(args.split, args.data)
    where = "" if args.data == DATA else f" from {args.data}, history from {DATA}"
    print(f"{len(scen)} incidents ({args.split}){where}, embedder={args.embedder}, k={K}")
    try:
        ev = evidence_eval(scen, args.embedder, args.data)
    except EmbedderUnavailable as e:
        print(f"error: {e}")
        return 2
    show("Evidence retrieval", ev["overall"], ["coverage", "all_keys", "mrr"])
    pa = paraphrase_eval(scen, args.embedder, args.data)
    show("Paraphrased queries (log-level ranking, top 5)", pa, ["coverage", "all_keys", "mrr"])
    hy = hypothesis_eval(scen, args.embedder, args.data, DATA)
    show("Root cause ranking", hy, ["top1", "top3", "mrr"])
    if args.label:
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"{args.label}-{args.split}.json"
        path.write_text(json.dumps({"split": args.split, "embedder": args.embedder, "k": K,
                                    "evidence": ev, "paraphrase": pa, "hypotheses": hy}, indent=1))
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
