import json
from pathlib import Path

import pytest

from agent.data import ScenarioData
from agent.investigator import run_investigation
from agent.llm import LLMResponse, MockLLM, ToolCall, _ollama_messages, _openai_messages
from agent.tools import Toolbox
from evals.run import load_manifest
from evals.score import score_run, summarize
from scenarios.generate import CATEGORIES, generate_all

DATA = Path(__file__).resolve().parent.parent / "scenarios" / "data"


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    out = tmp_path_factory.mktemp("scenarios")
    generate_all(out)
    return out


def _answer(root, sid):
    return json.loads((root / sid / "answer.json").read_text())


def test_generates_24_scenarios_with_split(scenarios):
    entries = load_manifest(scenarios, "all")
    assert len(entries) == 24
    assert {e["kind"] for e in entries} == set(CATEGORIES)
    assert len(load_manifest(scenarios, "dev")) == 12
    assert len(load_manifest(scenarios, "heldout")) == 12


def test_generation_is_deterministic(scenarios, tmp_path):
    generate_all(tmp_path)
    for name in ("logs.jsonl", "answer.json", "metrics.json"):
        a = (scenarios / "bad_deploy-1" / name).read_text()
        assert a == (tmp_path / "bad_deploy-1" / name).read_text()


def test_committed_data_matches_generator(scenarios):
    # Fails if someone edits the generator without regenerating scenarios/data.
    for name in ("alert.txt", "answer.json", "deploys.json"):
        assert (DATA / "memory_leak-2" / name).read_text() == (
            scenarios / "memory_leak-2" / name
        ).read_text()


def test_every_scenario_has_misleading_clue_and_noise(scenarios):
    for e in load_manifest(scenarios, "all"):
        data = ScenarioData(scenarios / e["id"])
        answer = _answer(scenarios, e["id"])
        assert len(data.logs) > 100
        assert len(data.deploys) >= 3
        assert answer["evidence_keys"]


def test_answer_is_not_reachable_through_tools(scenarios):
    data = ScenarioData(scenarios / "bad_deploy-0")
    answer = _answer(scenarios, "bad_deploy-0")
    assert not hasattr(data, "answer")
    blob = json.dumps([data.alert, data.logs, data.metrics, data.deploys, data.config_changes])
    assert "correct_action" not in blob and "evidence_keys" not in blob
    assert answer["incident_start"] not in data.alert  # alert is later than true start


def test_tools_are_read_only_and_filter(scenarios):
    box = Toolbox(ScenarioData(scenarios / "bad_deploy-0"))
    out = box.search_logs(query="nullpointer", level="ERROR", limit=3)
    assert "NullPointerException" in out and out.count("\n") == 3
    assert "checkout-service" in box.list_deploys(service="checkout-service")
    assert "metrics for" in box.query_metric(service="checkout-service")
    assert box.query_metric(service="nope").startswith("error")
    assert box.call("delete_everything", {}).startswith("error: unknown tool")
    assert box.call("search_logs", {"bogus": 1}).startswith("error: bad arguments")


def _report(answer, **over):
    base = {
        "root_cause_category": answer["root_cause_category"],
        "component": answer["component"],
        "confidence": "high",
        "summary": "s",
        "evidence": [f"log mentions {answer['evidence_keys'][0]}"],
        "action_type": answer["correct_action"]["type"],
        "action_target": answer["correct_action"]["target"],
    }
    base.update(over)
    return base


def test_agent_loop_scripted_correct_run(scenarios):
    sid = "bad_deploy-0"
    data, ans = ScenarioData(scenarios / sid), _answer(scenarios, sid)
    llm = MockLLM(
        [
            LLMResponse(tool_calls=[ToolCall("list_deploys", {})]),
            LLMResponse(tool_calls=[ToolCall("search_logs", {"level": "ERROR", "limit": 3})]),
            LLMResponse(tool_calls=[ToolCall("submit_report", _report(ans))]),
        ]
    )
    result = run_investigation(llm, data)
    assert result.stop_reason == "report" and result.steps == 2
    # tool results were fed back to the model with ids
    last = llm.calls[-1]
    assert last[-1]["role"] == "tool" and last[-2]["tool_calls"][0]["id"]
    row = score_run(result, ans)
    assert row["root_cause_correct"] and row["evidence_cited"] and not row["wrong_blame"]
    assert row["action_correct"]


def test_scoring_wrong_blame_vs_honest_unsure(scenarios):
    sid = "memory_leak-0"
    data, ans = ScenarioData(scenarios / sid), _answer(scenarios, sid)
    wrong = _report(ans, component="api-gateway", evidence=["nothing"])
    run = run_investigation(MockLLM([LLMResponse(tool_calls=[ToolCall("submit_report", wrong)])]), data)
    row = score_run(run, ans)
    assert not row["root_cause_correct"] and row["wrong_blame"] and not row["evidence_cited"]

    unsure = _report(ans, component="api-gateway", confidence="low")
    run = run_investigation(MockLLM([LLMResponse(tool_calls=[ToolCall("submit_report", unsure)])]), data)
    assert not score_run(run, ans)["wrong_blame"]


def test_invalid_report_is_rejected_then_retried(scenarios):
    sid = "feature_flag-0"
    data, ans = ScenarioData(scenarios / sid), _answer(scenarios, sid)
    bad = _report(ans, root_cause_category="gremlins")
    llm = MockLLM(
        [
            LLMResponse(tool_calls=[ToolCall("submit_report", bad)]),
            LLMResponse(tool_calls=[ToolCall("submit_report", _report(ans))]),
        ]
    )
    result = run_investigation(llm, data)
    assert result.stop_reason == "report"
    assert [t["type"] for t in result.trace] == ["report_rejected", "report"]


def test_step_limit_and_no_report_stops(scenarios):
    data = ScenarioData(scenarios / "bad_deploy-0")
    looping = MockLLM([LLMResponse(tool_calls=[ToolCall("list_deploys", {})])] * 3)
    result = run_investigation(looping, data, max_steps=3)
    assert result.stop_reason == "step_limit" and result.report is None and result.steps == 3

    chatty = MockLLM([LLMResponse(text="hmm")] * 3)
    assert run_investigation(chatty, data).stop_reason == "no_report"


def test_summarize_counts():
    rows = [
        {"root_cause_correct": True, "evidence_cited": True, "wrong_blame": False,
         "action_correct": True, "stop_reason": "report", "steps": 4,
         "input_tokens": 10, "output_tokens": 1},
        {"root_cause_correct": False, "evidence_cited": False, "wrong_blame": True,
         "action_correct": False, "stop_reason": "step_limit", "steps": 8,
         "input_tokens": 20, "output_tokens": 2},
    ]
    s = summarize(rows)
    assert (s["n"], s["root_cause_correct"], s["wrong_blame"], s["no_report"]) == (2, 1, 1, 1)
    assert s["avg_steps"] == 6.0 and s["input_tokens"] == 30


def test_backend_message_conversion():
    msgs = [
        {"role": "user", "content": "alert"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "list_deploys", "arguments": {"service": "x"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "list_deploys", "content": "rows"},
    ]
    oa = _openai_messages(msgs)
    assert oa[1]["tool_calls"][0]["function"]["arguments"] == '{"service": "x"}'
    assert oa[2] == {"role": "tool", "tool_call_id": "c1", "content": "rows"}
    ol = _ollama_messages(msgs)
    assert ol[1]["tool_calls"][0]["function"]["arguments"] == {"service": "x"}
    assert ol[2]["tool_name"] == "list_deploys"
