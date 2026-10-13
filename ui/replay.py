"""Turns a saved trace into readable replay steps. Pure functions, no UI framework."""

from __future__ import annotations

import json
from typing import Any

MAX_RESULT_CHARS = 1500


def _args(a: dict[str, Any]) -> str:
    return ", ".join(f"{k}={json.dumps(v)}" for k, v in a.items()) or "no arguments"


def describe_step(entry: dict[str, Any]) -> dict[str, str]:
    """One trace entry -> {"kind", "title", "body"} for display."""
    kind = entry["type"]
    if kind == "tool_call":
        result = entry["result"]
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + f"\n... ({len(entry['result'])} characters total)"
        title = f"Tool: {entry['name']}({_args(entry['arguments'])})"
        if entry.get("called_as"):
            title += f"  [model wrote {entry['called_as']!r}]"
        if entry.get("guard_redacted"):
            title += f"  [guard removed {len(entry['guard_redacted'])} line(s)]"
        return {"kind": kind, "title": title, "body": result}
    if kind == "model_text":
        return {"kind": kind, "title": "Agent note", "body": entry["text"]}
    if kind == "report":
        a = entry["arguments"]
        body = (
            f"Root cause: {a['root_cause_category']} in {a['component']} "
            f"(confidence {a['confidence']})\n{a.get('summary', '')}\n"
            f"Proposed action: {a['action_type']} -> {a.get('action_target', '')}"
        )
        return {"kind": kind, "title": "Final report", "body": body}
    if kind == "guard_block":
        return {"kind": kind, "title": "Report refused by guard", "body": entry["result"]}
    if kind == "report_rejected":
        return {"kind": kind, "title": "Report rejected (malformed)", "body": entry["result"]}
    return {"kind": kind, "title": kind, "body": json.dumps(entry)}


def replay_steps(trace: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"turn": str(e.get("turn", "")), **describe_step(e)} for e in trace]


def evidence_lines(report: dict[str, Any] | None) -> list[str]:
    return list(report.get("evidence", [])) if report else []
