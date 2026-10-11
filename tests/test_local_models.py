import json
import os
from pathlib import Path

import pytest

from agent.data import ScenarioData
from agent.env import load_dotenv
from agent.investigator import extract_text_tool_call, run_investigation
from agent.llm import LLMResponse, MockLLM, OllamaLLM, ToolCall, make_llm
from scenarios.generate import generate_all


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    out = tmp_path_factory.mktemp("lm")
    generate_all(out)
    return out


REPORT = {
    "root_cause_category": "bad_deploy",
    "component": "checkout-service",
    "confidence": "high",
    "summary": "s",
    "evidence": ["e"],
    "action_type": "rollback_deploy",
    "action_target": "checkout-service",
}


def test_recovers_tool_call_written_as_json_text():
    c = extract_text_tool_call('I will look. {"name": "search_logs", "parameters": {"level": "ERROR"}}')
    assert c == ToolCall("search_logs", {"level": "ERROR"})
    c = extract_text_tool_call(json.dumps({"name": "list_deploys", "arguments": "{}"}))
    assert c.name == "list_deploys" and c.arguments == {}


def test_recovers_bare_and_wrapped_report_text():
    assert extract_text_tool_call("Final: " + json.dumps(REPORT)).name == "submit_report"
    wrapped = {"name": "submit_report", "parameters": REPORT}
    assert extract_text_tool_call(json.dumps(wrapped)).arguments == REPORT


def test_ignores_unrelated_text():
    assert extract_text_tool_call("The root cause is a bad deploy.") is None
    assert extract_text_tool_call("{ broken json") is None
    assert extract_text_tool_call('{"name": "x"}') is None  # no arguments: not a tool call


def test_loop_accepts_report_written_as_text(root):
    data = ScenarioData(root / "bad_deploy-0")
    llm = MockLLM(
        [
            LLMResponse(text='{"name": "list_deploys", "parameters": {}}'),
            LLMResponse(text="Done. " + json.dumps(REPORT)),
        ]
    )
    result = run_investigation(llm, data)
    assert result.stop_reason == "report" and result.report == REPORT
    assert [t["type"] for t in result.trace].count("recovered_text_tool_call") == 2


def test_gives_up_only_after_three_plain_text_replies(root):
    data = ScenarioData(root / "bad_deploy-0")
    llm = MockLLM([LLMResponse(text="I think it is a deploy.")] * 3)
    result = run_investigation(llm, data)
    assert result.stop_reason == "no_report" and len(llm.calls) == 3
    assert "tool" in llm.calls[1][-1]["content"]  # the nudge explains what to do


def test_ollama_sends_context_window(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}}

    def fake_post(url, json=None, timeout=None):
        seen.update(json=json, timeout=timeout)
        return Resp()

    monkeypatch.setattr("agent.llm.httpx.post", fake_post)
    OllamaLLM("m", num_ctx=16384).chat([{"role": "user", "content": "q"}])
    assert seen["json"]["options"]["num_ctx"] == 16384 and seen["timeout"] >= 300
    llm = make_llm({"LLM_PROVIDER": "ollama", "LLM_MODEL": "m", "OLLAMA_NUM_CTX": "4096"})
    assert llm.num_ctx == 4096 and make_llm({"LLM_PROVIDER": "ollama"}).num_ctx == 8192


def test_dotenv_loads_but_never_overrides_real_env(tmp_path, monkeypatch):
    f = Path(tmp_path / ".env")
    f.write_text('# comment\nLLM_PROVIDER=ollama\nLLM_MODEL="qwen2.5:7b"\nEMPTY=\nKEEP=from_file\n')
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("KEEP", "from_shell")
    keys = load_dotenv(f)
    assert sorted(keys) == ["LLM_MODEL", "LLM_PROVIDER"]
    assert os.environ["LLM_MODEL"] == "qwen2.5:7b" and os.environ["KEEP"] == "from_shell"
    assert load_dotenv(tmp_path / "missing.env") == []
    for k in ("LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_show_trace_prints_steps(tmp_path, root, capsys):
    from evals.show_trace import main

    data = ScenarioData(root / "bad_deploy-0")
    llm = MockLLM([LLMResponse(tool_calls=[ToolCall("list_deploys", {})]), LLMResponse(text=json.dumps(REPORT))])
    result = run_investigation(llm, data)
    path = tmp_path / "t.json"
    result.save(path)
    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "list_deploys" in out and "Final report" in out


def test_invented_tool_names_are_matched_to_the_closest_real_tool(root):
    from agent.tools import resolve_tool_name

    assert resolve_tool_name("list_recent_deploys") == "list_deploys"
    assert resolve_tool_name("search_log") == "search_logs"
    assert resolve_tool_name("submit_final_report") == "submit_report"
    assert resolve_tool_name("rm_rf") is None

    data = ScenarioData(root / "bad_deploy-1")
    llm = MockLLM(
        [
            LLMResponse(text='{"name": "list_recent_deploys", "parameters": {"start": "2026-03-09"}}'),
            LLMResponse(tool_calls=[ToolCall("submit_report", REPORT)]),
        ]
    )
    result = run_investigation(llm, data)
    call = next(t for t in result.trace if t["type"] == "tool_call")
    assert call["name"] == "list_deploys" and call["called_as"] == "list_recent_deploys"
    assert "ignored unknown arguments ['start']" in call["result"]  # and still returned deploys
    assert result.stop_reason == "report"


def test_truly_unknown_tool_gets_an_error_listing_valid_tools(root):
    data = ScenarioData(root / "bad_deploy-1")
    llm = MockLLM(
        [
            LLMResponse(text='{"name": "rm_rf", "parameters": {}}'),
            LLMResponse(tool_calls=[ToolCall("submit_report", REPORT)]),
        ]
    )
    result = run_investigation(llm, data)
    call = next(t for t in result.trace if t["type"] == "tool_call")
    assert "unknown tool" in call["result"] and "search_logs" in call["result"]


def test_tools_tolerate_null_and_extra_arguments(root):
    from agent.tools import Toolbox

    box = Toolbox(ScenarioData(root / "bad_deploy-0"))
    out = box.call("search_logs", {"query": None, "level": "ERROR", "service": None, "limit": "3"})
    assert out.startswith("note") is False and "matching lines, showing 3" in out
    assert "metrics for" in box.call("query_metric", {"service": "checkout-service", "extra": 1})
    assert box.call("query_metric", {}).startswith("error: bad arguments")
