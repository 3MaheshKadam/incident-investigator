"""Read-only evidence tools. None of these can change anything."""

from __future__ import annotations

import difflib
import inspect
from collections import Counter
from typing import Any

from agent.data import ScenarioData

CATEGORIES = [
    "bad_deploy",
    "memory_leak",
    "expired_certificate",
    "slow_query",
    "feature_flag",
    "dependency_outage",
]
ACTION_TYPES = ["rollback_deploy", "restart_service", "toggle_feature_flag", "open_ticket"]
CONFIDENCES = ["low", "medium", "high"]
MAX_LOG_LINES = 40

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_logs",
        "description": (
            "Search log lines. All filters are optional. Returns matching lines in time order."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive text to find"},
                "service": {"type": "string", "description": "Only this service"},
                "level": {"type": "string", "enum": ["INFO", "WARN", "ERROR"]},
                "start": {"type": "string", "description": "ISO time, inclusive"},
                "end": {"type": "string", "description": "ISO time, inclusive"},
                "limit": {"type": "integer", "description": f"Max lines (up to {MAX_LOG_LINES})"},
            },
        },
    },
    {
        "name": "query_metric",
        "description": (
            "Get a metric time series (5-minute points) for a service. Metrics: error_rate (%), "
            "latency_p95_ms, memory_mb, cpu_pct, request_rate. Call with only service to list "
            "which metrics exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "metric": {"type": "string"},
                "start": {"type": "string", "description": "ISO time, inclusive"},
                "end": {"type": "string", "description": "ISO time, inclusive"},
            },
            "required": ["service"],
        },
    },
    {
        "name": "list_deploys",
        "description": "List recent deploys (time, service, version, author, commit message).",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "Only this service"}},
        },
    },
    {
        "name": "list_config_changes",
        "description": "List recent configuration and feature flag changes.",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "Only this service"}},
        },
    },
    {
        "name": "submit_report",
        "description": (
            "Finish the investigation. Call this exactly once when you can name the root cause "
            "and cite evidence, or when you cannot and want to say so with low confidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause_category": {"type": "string", "enum": CATEGORIES},
                "component": {
                    "type": "string",
                    "description": "The service or system that is the root cause",
                },
                "confidence": {"type": "string", "enum": CONFIDENCES},
                "summary": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact log lines, deploys, config changes or metric facts",
                },
                "action_type": {"type": "string", "enum": ACTION_TYPES},
                "action_target": {"type": "string"},
            },
            "required": [
                "root_cause_category",
                "component",
                "confidence",
                "summary",
                "evidence",
                "action_type",
                "action_target",
            ],
        },
    },
]


def resolve_tool_name(name: str) -> str | None:
    """Map a tool name to a real one. Weak models invent near-misses like list_recent_deploys."""
    names = [t["name"] for t in TOOL_SPECS]
    if name in names:
        return name
    close = difflib.get_close_matches(str(name), names, n=1, cutoff=0.7)
    return close[0] if close else None


def validate_report(args: dict[str, Any]) -> str | None:
    """Return every problem with the report at once (so a weak model fixes it in one go)."""
    problems = []
    for key in ("root_cause_category", "component", "confidence", "action_type"):
        if not args.get(key):
            problems.append(f"missing field {key}")
    if args.get("root_cause_category") and args["root_cause_category"] not in CATEGORIES:
        problems.append(f"root_cause_category must be one of {CATEGORIES}")
    if args.get("confidence") and args["confidence"] not in CONFIDENCES:
        problems.append(f"confidence must be one of {CONFIDENCES}")
    if args.get("action_type") and args["action_type"] not in ACTION_TYPES:
        problems.append(f"action_type must be one of {ACTION_TYPES}")
    if not isinstance(args.get("evidence", []), list):
        problems.append("evidence must be a list of strings")
    if not problems:
        return None
    return (
        "; ".join(problems) + ". Required fields: root_cause_category, component, confidence, "
        "summary, evidence (list), action_type, action_target."
    )


class Toolbox:
    """Runs the read-only tools against one scenario."""

    def __init__(self, data: ScenarioData):
        self.data = data

    def call(self, name: str, args: dict[str, Any]) -> str:
        fn = {
            "search_logs": self.search_logs,
            "query_metric": self.query_metric,
            "list_deploys": self.list_deploys,
            "list_config_changes": self.list_config_changes,
        }.get(name)
        if fn is None:
            valid = [t["name"] for t in TOOL_SPECS]
            return f"error: unknown tool {name!r}. Available tools: {valid}"
        # Weak models add arguments the tool does not take, or send null for optional ones.
        accepted = inspect.signature(fn).parameters
        clean = {k: v for k, v in args.items() if k in accepted and v is not None}
        ignored = sorted(set(args) - set(clean) - {k for k, v in args.items() if v is None})
        try:
            out = fn(**clean)
        except (TypeError, ValueError, AttributeError) as e:
            return f"error: bad arguments for {name}: {e}"
        return f"note: ignored unknown arguments {ignored}\n{out}" if ignored else out

    def search_logs(
        self,
        query: str = "",
        service: str | None = None,
        level: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 20,
    ) -> str:
        limit = max(1, min(int(limit), MAX_LOG_LINES))
        q = query.lower()
        hits = [
            r
            for r in self.data.logs
            if (not q or q in r["msg"].lower())
            and (not service or r["service"] == service)
            and (not level or r["level"] == level)
            and (not start or r["ts"] >= start)
            and (not end or r["ts"] <= end)
        ]
        counts = Counter(r["level"] for r in hits)
        breakdown = ", ".join(f"{lv} {counts[lv]}" for lv in ("ERROR", "WARN", "INFO") if counts[lv])
        latest = list(reversed(hits[-limit:]))  # newest first, like tailing a log
        lines = [f"{r['ts']} {r['level']} {r['service']}: {r['msg']}" for r in latest]
        header = (
            f"{len(hits)} matching lines ({breakdown or 'none'}), "
            f"showing the {len(lines)} most recent, newest first"
        )
        return "\n".join([header, *lines])

    def query_metric(
        self,
        service: str,
        metric: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> str:
        series = self.data.metrics.get(service)
        if series is None:
            return f"error: no metrics for {service!r}. Services: {sorted(self.data.metrics)}"
        if metric is None:
            return f"metrics for {service}: {sorted(series)}"
        if metric not in series:
            return f"error: unknown metric {metric!r}. Metrics: {sorted(series)}"
        pts = [
            (t, v)
            for t, v in series[metric]
            if (not start or t >= start) and (not end or t <= end)
        ]
        if not pts:
            return f"{service} {metric}: no points in that time range"
        top = max(pts, key=lambda p: p[1])
        summary = f"summary: min {min(v for _, v in pts)}, max {top[1]} at {top[0]}, last {pts[-1][1]}"
        return "\n".join(
            [f"{service} {metric} ({len(pts)} points)", summary] + [f"{t} {v}" for t, v in pts]
        )

    def list_deploys(self, service: str | None = None) -> str:
        rows = [d for d in self.data.deploys if not service or d["service"] == service]
        if not rows:
            return "no deploys"
        return "\n".join(
            f"{d['ts']} {d['service']} {d['version']} by {d['author']}: {d['commit_message']}"
            for d in rows
        )

    def list_config_changes(self, service: str | None = None) -> str:
        rows = [c for c in self.data.config_changes if not service or c["service"] == service]
        if not rows:
            return "no config changes"
        return "\n".join(
            f"{c['ts']} {c['service']} {c['key']}: {c['old']} -> {c['new']} by {c['author']}"
            for c in rows
        )
