"""Glue: investigate, save the run, and put the proposed action in front of a human."""

from __future__ import annotations

from dataclasses import dataclass

from agent.data import ScenarioData
from agent.investigator import GuardLike, RunResult, run_investigation
from agent.llm import LLM
from agent.remediation import ApprovalGate, SimulatedSystem
from agent.store import Store


@dataclass
class PipelineOutcome:
    run_id: int
    proposal_id: int | None
    result: RunResult


def investigate_and_propose(
    llm: LLM,
    data: ScenarioData,
    store: Store,
    gate: ApprovalGate,
    max_steps: int = 12,
    guard: GuardLike | None = None,
) -> PipelineOutcome:
    result = run_investigation(llm, data, max_steps=max_steps, guard=guard)
    run_id = store.save_run(result)
    pid = gate.propose(data.id, result.report, run_id) if result.report else None
    return PipelineOutcome(run_id, pid, result)


def new_gate(store: Store, data: ScenarioData) -> ApprovalGate:
    return ApprovalGate(store, SimulatedSystem.from_data(data))
