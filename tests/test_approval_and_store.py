import json

import pytest

from agent.data import ScenarioData
from agent.pipeline import investigate_and_propose, new_gate
from agent.remediation import ActionError, ApprovalGate, SimulatedSystem, needs_human
from agent.store import Store
from scenarios.generate import generate_all
from tests.fakes import OracleLLM
from ui.replay import describe_step, evidence_lines, replay_steps


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    out = tmp_path_factory.mktemp("s")
    generate_all(out)
    return out


def oracle(root, sid):
    return OracleLLM(json.loads((root / sid / "answer.json").read_text()))


def test_risky_action_waits_for_human_and_changes_nothing_until_approved(root):
    data, store = ScenarioData(root / "bad_deploy-0"), Store()
    gate = new_gate(store, data)
    before = dict(gate.system.versions)
    out = investigate_and_propose(oracle(root, data.id), data, store, gate)
    p = store.get_proposal(out.proposal_id)
    assert p["action_type"] == "rollback_deploy" and p["status"] == "pending"
    assert gate.system.versions == before  # nothing happened yet
    msg = gate.approve(out.proposal_id, "reviewer")
    assert "rolled back" in msg and gate.system.versions != before
    assert store.get_proposal(out.proposal_id)["status"] == "approved"
    with pytest.raises(ActionError):
        gate.approve(out.proposal_id, "reviewer")  # cannot decide twice


def test_reject_does_not_execute_and_is_audited(root):
    data, store = ScenarioData(root / "memory_leak-0"), Store()
    gate = new_gate(store, data)
    out = investigate_and_propose(oracle(root, data.id), data, store, gate)
    gate.reject(out.proposal_id, "reviewer", "not now")
    assert gate.system.restarts == {}
    events = [a["event"] for a in store.list_audit()]
    assert "proposed" in events and "rejected" in events and "run_saved" in events


def test_low_risk_ticket_is_auto_approved_and_logged(root):
    data, store = ScenarioData(root / "expired_certificate-0"), Store()
    gate = new_gate(store, data)
    out = investigate_and_propose(oracle(root, data.id), data, store, gate)
    p = store.get_proposal(out.proposal_id)
    assert p["status"] == "auto_approved" and p["decided_by"] == "policy"
    assert len(gate.system.tickets) == 1
    assert not needs_human("open_ticket") and needs_human("restart_service")


def test_unknown_action_is_refused():
    gate = ApprovalGate(Store(), SimulatedSystem())
    with pytest.raises(ActionError):
        gate.propose("x", {"action_type": "drop_database", "action_target": "db"})
    with pytest.raises(ActionError):
        SimulatedSystem().apply("drop_database", "db")


def test_store_round_trip_and_replay(root):
    data, store = ScenarioData(root / "feature_flag-1"), Store()
    out = investigate_and_propose(oracle(root, data.id), data, store, new_gate(store, data))
    run = store.get_run(out.run_id)
    assert run["report"]["root_cause_category"] == "feature_flag"
    assert store.list_runs()[0]["id"] == out.run_id
    steps = replay_steps(run["trace"])
    assert steps[0]["title"].startswith("Tool: list_deploys")
    assert steps[-1]["title"] == "Final report"
    assert evidence_lines(run["report"])
    assert describe_step({"type": "guard_block", "result": "no"})["title"].startswith("Report refused")
