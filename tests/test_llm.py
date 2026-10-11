import pytest

from agent.llm import (
    LLMResponse,
    MockLLM,
    OllamaLLM,
    OpenAICompatLLM,
    ToolCall,
    make_llm,
)


def test_mock_returns_script_in_order_and_records_calls():
    llm = MockLLM([LLMResponse(text="a"), LLMResponse(tool_calls=[ToolCall("search_logs", {})])])
    assert llm.chat([{"role": "user", "content": "hi"}]).text == "a"
    assert llm.chat([]).tool_calls[0].name == "search_logs"
    assert len(llm.calls) == 2


def test_mock_raises_when_script_runs_out():
    with pytest.raises(RuntimeError):
        MockLLM([]).chat([])


def test_make_llm_defaults_to_mock():
    assert isinstance(make_llm({}), MockLLM)


def test_make_llm_ollama():
    llm = make_llm({"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3.1:8b"})
    assert isinstance(llm, OllamaLLM)
    assert llm.model == "llama3.1:8b"


def test_make_llm_hosted_requires_key_and_url():
    with pytest.raises(ValueError):
        make_llm({"LLM_PROVIDER": "openai_compat", "LLM_MODEL": "x"})
    llm = make_llm(
        {
            "LLM_PROVIDER": "openai_compat",
            "LLM_MODEL": "x",
            "LLM_BASE_URL": "https://api.example.com/v1",
            "LLM_API_KEY": "k",
        }
    )
    assert isinstance(llm, OpenAICompatLLM)


def test_make_llm_unknown_provider():
    with pytest.raises(ValueError):
        make_llm({"LLM_PROVIDER": "nope"})


def test_openai_compat_parses_tool_calls(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"function": {"name": "query_metric", "arguments": '{"m": "cpu"}'}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            }

    monkeypatch.setattr("agent.llm.httpx.post", lambda *a, **k: FakeResp())
    out = OpenAICompatLLM("m", "https://x/v1", "k").chat([{"role": "user", "content": "q"}])
    assert out.tool_calls == [ToolCall("query_metric", {"m": "cpu"})]
    assert (out.input_tokens, out.output_tokens) == (10, 3)


def test_ollama_parses_response(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": {
                    "content": "ok",
                    "tool_calls": [{"function": {"name": "list_deploys", "arguments": {}}}],
                },
                "prompt_eval_count": 7,
                "eval_count": 2,
            }

    monkeypatch.setattr("agent.llm.httpx.post", lambda *a, **k: FakeResp())
    out = OllamaLLM("m").chat([{"role": "user", "content": "q"}])
    assert out.text == "ok" and out.tool_calls[0].name == "list_deploys"
    assert (out.input_tokens, out.output_tokens) == (7, 2)
