"""Automatic scoring of a run against the hidden answer. Plain code, no judge model."""

from __future__ import annotations

from typing import Any

from agent.investigator import RunResult


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def score_run(result: RunResult, answer: dict[str, Any]) -> dict[str, Any]:
    report = result.report
    row: dict[str, Any] = {
        "id": result.scenario_id,
        "kind": answer["kind"],
        "stop_reason": result.stop_reason,
        "steps": result.steps,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "root_cause_correct": False,
        "evidence_cited": False,
        "wrong_blame": False,
        "action_correct": False,
    }
    if report is None:
        return row

    component_ok = _norm(report["component"]) == _norm(answer["component"])
    category_ok = _norm(report["root_cause_category"]) == _norm(answer["root_cause_category"])
    row["root_cause_correct"] = component_ok and category_ok

    evidence = " ".join(_norm(e) for e in report.get("evidence", []))
    row["evidence_cited"] = any(_norm(k) in evidence for k in answer["evidence_keys"])

    # Blaming the wrong component while claiming medium/high confidence.
    row["wrong_blame"] = (not component_ok) and report["confidence"] != "low"
    row["action_correct"] = _norm(report["action_type"]) == _norm(answer["correct_action"]["type"])
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    count = lambda key: sum(1 for r in rows if r[key])
    return {
        "n": n,
        "root_cause_correct": count("root_cause_correct"),
        "evidence_cited": count("evidence_cited"),
        "wrong_blame": count("wrong_blame"),
        "action_correct": count("action_correct"),
        "no_report": sum(1 for r in rows if r["stop_reason"] != "report"),
        "avg_steps": round(sum(r["steps"] for r in rows) / n, 2) if n else 0.0,
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }
