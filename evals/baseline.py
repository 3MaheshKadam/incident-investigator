"""A rule-based, non-LLM investigator used as a floor and to smoke-test the harness in CI.

It follows a fixed tool sequence and picks a diagnosis from keyword rules. It never reads text as
instructions, so it cannot be hijacked, and its scores say nothing about a real model. If it scores
very high, the scenarios are too easy to separate good agents from bad ones.

Run:  python -m evals.baseline
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from agent.data import ScenarioData
from agent.investigator import run_investigation
from agent.llm import LLMResponse, Message, ToolCall, ToolSpec
from evals.run import DATA_DIR, load_manifest
from evals.score import score_run, summarize

SEQUENCE = [
    ("list_deploys", {}),
    ("search_logs", {"level": "ERROR", "limit": 40}),
    ("list_config_changes", {}),
    ("search_logs", {"level": "WARN", "limit": 40}),
]
LINE = re.compile(r"^(\S+) (INFO|WARN|ERROR) (\S+): (.*)$")


def _service_of(text: str, needle: str) -> str | None:
    for line in text.splitlines():
        m = LINE.match(line)
        if m and needle in m.group(4):
            return m.group(3)
    return None


def decide(text: str) -> dict:
    def report(category, component, action, target, evidence):
        return {
            "root_cause_category": category,
            "component": component,
            "confidence": "medium",
            "summary": f"Rule-based diagnosis: {category} in {component}",
            "evidence": [evidence],
            "action_type": action,
            "action_target": target,
        }

    for needle, category, action in [
        ("certificate has expired", "expired_certificate", "open_ticket"),
        ("OOMKilled", "memory_leak", "restart_service"),
        ("slow query", "slow_query", "open_ticket"),
    ]:
        svc = _service_of(text, needle)
        if svc:
            return report(category, svc, action, svc, needle)

    m = re.search(r"upstream (\S+): connection refused", text)
    if m:
        return report("dependency_outage", m.group(1).rstrip(":"), "open_ticket", m.group(1).rstrip(":"),
                      "connection refused")

    svc = _service_of(text, "experimental code path")
    if svc:
        flags = re.findall(r"^\S+ \S+ (\S+): -> ?", text, re.MULTILINE)  # unused; flag read below
        del flags
        flag = ""
        for line in text.splitlines():
            m2 = re.match(r"^\S+ " + re.escape(svc) + r" (\S+): off -> on", line)
            if m2:
                flag = m2.group(1)
        return report("feature_flag", svc, "toggle_feature_flag", flag or svc, flag or "experimental code path")

    errors: dict[str, int] = {}
    for line in text.splitlines():
        m3 = LINE.match(line)
        if m3 and m3.group(2) == "ERROR":
            errors[m3.group(3)] = errors.get(m3.group(3), 0) + 1
    svc = max(errors, key=errors.get) if errors else "unknown"
    return report("bad_deploy", svc, "rollback_deploy", svc, "most error lines")


class RuleBasedLLM:
    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None) -> LLMResponse:
        done = [m for m in messages if m["role"] == "tool"]
        if len(done) < len(SEQUENCE):
            name, args = SEQUENCE[len(done)]
            return LLMResponse(tool_calls=[ToolCall(name, args)])
        text = "\n".join(m["content"] for m in done)
        return LLMResponse(tool_calls=[ToolCall("submit_report", decide(text))])


def main() -> int:
    rows = []
    for e in load_manifest(DATA_DIR, "all"):
        folder = Path(DATA_DIR) / e["id"]
        result = run_investigation(RuleBasedLLM(), ScenarioData(folder))
        rows.append(score_run(result, json.loads((folder / "answer.json").read_text())))
    s = summarize(rows)
    n = s["n"]
    print(f"rule-based baseline on {n} scenarios: root cause {s['root_cause_correct']}/{n}, "
          f"evidence {s['evidence_cited']}/{n}, wrong blame {s['wrong_blame']}/{n}, "
          f"no report {s['no_report']}/{n}, avg steps {s['avg_steps']}")
    return 1 if s["no_report"] else 0  # pipeline smoke test: every run must end in a report


if __name__ == "__main__":
    sys.exit(main())
