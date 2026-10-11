"""The investigator loop: alert in, evidence gathered with read-only tools, report out."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.data import ScenarioData
from agent.llm import LLM, Message
from agent.tools import TOOL_SPECS, Toolbox, validate_report

SYSTEM_PROMPT = """You are an on-call engineer investigating a production incident.

You start from an alert. Use the read-only tools to gather evidence: search logs, query metrics,
list recent deploys and config changes. Form a hypothesis, check it against the evidence, and
look for clues that contradict it. Some clues are misleading: a recent change or a warning is
not automatically the cause.

When you can name the root cause and cite evidence, call submit_report exactly once. Cite exact
log lines, deploys, config changes or metric facts as evidence. If you cannot find a cause,
submit a report with confidence "low" instead of guessing confidently. You can only propose one
action; a human will review it before anything happens."""

NUDGE = "Use the tools to investigate, then call submit_report when you are done."


@dataclass
class RunResult:
    scenario_id: str
    report: dict[str, Any] | None
    steps: int  # evidence tool calls (submit_report not counted)
    stop_reason: str  # "report" | "step_limit" | "no_report"
    trace: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=1) + "\n")


def run_investigation(llm: LLM, data: ScenarioData, max_steps: int = 12) -> RunResult:
    """Run one investigation. `max_steps` caps model turns, so a looping model stops."""
    box = Toolbox(data)
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Alert:\n{data.alert}"},
    ]
    result = RunResult(data.id, None, 0, "step_limit")
    nudges = 0
    call_counter = 0

    for turn in range(1, max_steps + 1):
        resp = llm.chat(messages, TOOL_SPECS)
        result.input_tokens += resp.input_tokens
        result.output_tokens += resp.output_tokens
        if resp.text:
            result.trace.append({"turn": turn, "type": "model_text", "text": resp.text})

        if not resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.text})
            nudges += 1
            if nudges >= 2:
                result.stop_reason = "no_report"
                return result
            messages.append({"role": "user", "content": NUDGE})
            continue

        calls = []
        for c in resp.tool_calls:
            call_counter += 1
            calls.append(
                {"id": c.id or f"call_{call_counter}", "name": c.name, "arguments": c.arguments}
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
                if error is None:
                    result.report = c["arguments"]
                    result.stop_reason = "report"
                    return result
                output = f"error: {error}"
            else:
                output = box.call(c["name"], c["arguments"])
                result.steps += 1
                result.trace.append(
                    {
                        "turn": turn,
                        "type": "tool_call",
                        "name": c["name"],
                        "arguments": c["arguments"],
                        "result": output,
                    }
                )
            messages.append(
                {"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": output}
            )

    return result
