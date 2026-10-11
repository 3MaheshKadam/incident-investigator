"""The investigator loop: alert in, evidence gathered with read-only tools, report out."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent.data import ScenarioData
from agent.llm import LLM, Message, ToolCall
from agent.tools import TOOL_SPECS, Toolbox, resolve_tool_name, validate_report

SYSTEM_PROMPT = """You are an on-call engineer investigating a production incident.

You start from an alert. Use the read-only tools to gather evidence: search logs, query metrics,
list recent deploys and config changes. Form a hypothesis, check it against the evidence, and
look for clues that contradict it. Some clues are misleading: a recent change or a warning is
not automatically the cause.

A good order: (1) list recent deploys and config changes and note anything shortly before the
alert; (2) search ERROR logs, then WARN logs, for the alerting service and any service it
depends on; (3) query metrics to find exactly when the problem started and which service
changed first. Only state facts that tool results actually showed. Never invent log lines,
metric values or timestamps.

When you can name the root cause and cite evidence, call submit_report exactly once. Cite exact
log lines, deploys, config changes or metric facts as evidence. If you cannot find a cause,
submit a report with confidence "low" instead of guessing confidently. You can only propose one
action; a human will review it before anything happens.

Always respond by calling a tool. Never write your answer as plain text."""

NUDGE = (
    "You must reply by calling a tool, not with plain text. Use the evidence tools to keep "
    "investigating, or call submit_report with root_cause_category, component, confidence, "
    "summary, evidence (a list), action_type and action_target."
)
MAX_NUDGES = 3


def extract_text_tool_call(text: str) -> ToolCall | None:
    """Recover a tool call a weak model wrote as JSON text instead of a real tool call.

    Small local models often print {"name": "search_logs", "parameters": {...}} or a bare report
    object. Accepting these keeps a formatting slip from counting as a failed investigation.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters"))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        if isinstance(name, str) and isinstance(args, dict):
            return ToolCall(name, args)
        if "root_cause_category" in obj:
            return ToolCall("submit_report", obj)
    return None


class GuardLike(Protocol):
    """What the loop needs from a guard (see guard/guard.py). Optional."""

    system_addendum: str

    def begin(self, alert: str) -> str: ...
    def wrap_output(self, tool_name: str, output: str) -> tuple[str, list[str]]: ...
    def check_report(self, report: dict[str, Any]) -> str | None: ...


@dataclass
class RunResult:
    scenario_id: str
    report: dict[str, Any] | None
    steps: int  # evidence tool calls (submit_report not counted)
    stop_reason: str  # "report" | "step_limit" | "no_report"
    trace: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    guard_redactions: int = 0  # tool-output lines the guard removed as injected instructions
    guard_blocks: int = 0  # reports the guard refused

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=1) + "\n")


def _window(data: ScenarioData) -> str:
    """Tell the model what time range the data covers, so it does not query the wrong days."""
    stamps = [r["ts"] for r in data.logs]
    if not stamps:
        return ""
    return f"\n\nData is available from {min(stamps)} to {max(stamps)} (UTC)."


def run_investigation(
    llm: LLM, data: ScenarioData, max_steps: int = 12, guard: GuardLike | None = None
) -> RunResult:
    """Run one investigation. `max_steps` caps model turns, so a looping model stops.

    With a guard, everything read from tools and the alert is passed through it as untrusted
    data, and the final report must pass its checks before it is accepted.
    """
    box = Toolbox(data)
    system = SYSTEM_PROMPT + ("\n\n" + guard.system_addendum if guard else "")
    alert = guard.begin(data.alert) if guard else data.alert
    messages: list[Message] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Alert:\n{alert}{_window(data)}"},
    ]
    result = RunResult(data.id, None, 0, "step_limit")
    if guard is not None:
        result.guard_redactions += len(getattr(guard, "alert_flags", []))
    nudges = 0
    call_counter = 0

    for turn in range(1, max_steps + 1):
        resp = llm.chat(messages, TOOL_SPECS)
        result.input_tokens += resp.input_tokens
        result.output_tokens += resp.output_tokens
        if resp.text:
            result.trace.append({"turn": turn, "type": "model_text", "text": resp.text})

        if not resp.tool_calls and resp.text:
            recovered = extract_text_tool_call(resp.text)
            if recovered:
                resp.tool_calls = [recovered]
                result.trace.append(
                    {"turn": turn, "type": "recovered_text_tool_call", "name": recovered.name}
                )

        if not resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.text})
            nudges += 1
            if nudges >= MAX_NUDGES:
                result.stop_reason = "no_report"
                return result
            messages.append({"role": "user", "content": NUDGE})
            continue

        calls = []
        for c in resp.tool_calls:
            call_counter += 1
            calls.append(
                {
                    "id": c.id or f"call_{call_counter}",
                    "name": resolve_tool_name(c.name) or c.name,
                    "called_as": c.name,
                    "arguments": c.arguments,
                }
            )
        messages.append({"role": "assistant", "content": resp.text, "tool_calls": calls})

        for c in calls:
            if c["name"] == "submit_report":
                error = validate_report(c["arguments"])
                result.trace.append(
                    {
                        "turn": turn,
                        "type": "report" if error is None else "report_rejected",
                        "arguments": c["arguments"],
                        "result": error or "accepted",
                    }
                )
                if error is None and guard is not None:
                    reason = guard.check_report(c["arguments"])
                    if reason:
                        error = f"blocked by guard: {reason}"
                        result.guard_blocks += 1
                        result.trace[-1].update(type="guard_block", result=error)
                if error is None:
                    result.report = c["arguments"]
                    result.stop_reason = "report"
                    return result
                output = f"error: {error}"
            else:
                output = box.call(c["name"], c["arguments"])
                flagged: list[str] = []
                if guard is not None:
                    output, flagged = guard.wrap_output(c["name"], output)
                    result.guard_redactions += len(flagged)
                result.steps += 1
                entry = {
                    "turn": turn,
                    "type": "tool_call",
                    "name": c["name"],
                    "arguments": c["arguments"],
                    "result": output,
                }
                if c["called_as"] != c["name"]:
                    entry["called_as"] = c["called_as"]
                if flagged:
                    entry["guard_redacted"] = flagged
                result.trace.append(entry)
            messages.append(
                {"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": output}
            )

    return result
