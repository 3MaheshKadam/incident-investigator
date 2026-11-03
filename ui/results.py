"""Load the committed measurement files into plain tables for the results page.

Every loader returns an empty result when its file is missing, so the page shows what has been
measured so far and says what has not been run yet.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERF = ROOT / "perf" / "results"
RETRIEVAL = ROOT / "retrieval" / "results"
EVAL_REPORTS = ROOT / "evals" / "reports"


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def detection_rows(path: Path = PERF / "live-detect-after.json") -> list[dict]:
    """One row per real run: an injected fault (detected or not) or a healthy run (false alarms)."""
    rows = []
    for r in _load(path) or []:
        if "detected" in r:
            rows.append({"run": r["kind"], "injected_fault": True, "detected": r["detected"],
                         "samples_to_detect": r.get("ttd_samples"),
                         "extra_incidents": r.get("extra_incidents", 0)})
        else:
            rows.append({"run": r["kind"], "injected_fault": False, "detected": None,
                         "samples_to_detect": None, "extra_incidents": r.get("false_alarms", 0)})
    return rows


def detection_summary(rows: list[dict]) -> dict:
    faults = [r for r in rows if r["injected_fault"]]
    return {"faults": len(faults), "detected": sum(1 for r in faults if r["detected"]),
            "healthy_runs": len(rows) - len(faults),
            "false_incidents": sum(r["extra_incidents"] for r in rows)}


def pool_sweep(directory: Path = PERF) -> list[dict]:
    """Throughput and tail latency at each database pool size and client concurrency."""
    rows = []
    for path in sorted(directory.glob("poolsize-*.json")):
        data = _load(path)
        for r in (data or {}).get("results", []):
            rows.append({"run": data["label"], "concurrency": r["concurrency"], "rps": r["rps"],
                         "p95_ms": r["p95_ms"], "error_pct": r["error_pct"]})
    return rows


def retrieval_evidence(directory: Path = RETRIEVAL) -> list[dict]:
    """Share of needed evidence found, per way of choosing log lines, per embedder."""
    rows = []
    for path in sorted(directory.glob("*heldout*.json")):
        data = _load(path)
        if not data:
            continue
        for method, m in data["evidence"]["overall"].items():
            rows.append({"file": path.stem, "embedder": data["embedder"], "method": method,
                         "coverage": m["coverage"], "n": m["n"]})
    return rows


def retrieval_hypotheses(directory: Path = RETRIEVAL) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*heldout*.json")):
        data = _load(path)
        for method, m in (data or {}).get("hypotheses", {}).items():
            rows.append({"file": path.stem, "method": method, "top1": m["top1"], "top3": m["top3"],
                         "n": m.get("n")})
    return rows


def poisoning_rows(path: Path = RETRIEVAL / "history-poison.json") -> list[dict]:
    """How often a planted past incident wins the similar-incident list, and what it changes."""
    summary = (_load(path) or {}).get("summary", {})
    return [
        {"attack": name, "planted_incident_wins": s["hijacked"], "flagged": s["flagged_when_hijacked"],
         "runbook_changed": s["runbook_changed"], "injected_text_in_tool_output": s["leaked"],
         "fused_top1_when_poisoned": s["fused_top1_poisoned"]}
        for name, s in summary.items()
    ]


def model_runs(directory: Path = EVAL_REPORTS) -> list[str]:
    """Names of model-evaluation report folders, empty until those runs have been made."""
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir())
