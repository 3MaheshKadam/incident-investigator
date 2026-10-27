import json
from pathlib import Path

import httpx
import pytest

from agent.data import ScenarioData
from retrieval.bm25 import BM25
from retrieval.docs import build_docs, parse_alert
from retrieval.embed import NgramEmbedder, OllamaEmbedder, cosine, hash_str, make_embedder
from retrieval.evidence import EvidenceRetriever
from retrieval.fusion import rank_order, rrf
from retrieval.kb import KnowledgeBase, signature
from retrieval.text import tokens
from retrieval.tools import RetrievalToolbox

DATA = Path(__file__).resolve().parent.parent / "scenarios" / "data"


def test_tokens_keep_identifiers_whole_and_split():
    t = tokens("NullPointerException in new_ranking_model v1.6.0, pods restarted")
    assert {"nullpointerexception", "null", "pointer", "new_ranking_model", "rank", "v1.6.0", "restart"} <= set(t)


def test_bm25_prefers_the_rare_matching_document():
    docs = ["cache hit ratio 0.93", "connection pool 6/20 in use", "x509 certificate has expired", "cache warm"]
    s = BM25(docs).scores("certificate expired")
    assert s.index(max(s)) == 2 and s[0] == 0


def test_rrf_rewards_agreement_and_rank_order_drops_zero_scores():
    assert rank_order([0.0, 2.0, 1.0]) == [1, 2]
    fused = rrf([[0, 1, 2], [2, 0, 1]])
    assert fused[0] > fused[1]


def test_ngram_embedder_is_stable_and_tolerates_variants():
    assert hash_str("abc") == hash_str("abc")
    e = NgramEmbedder()
    a, b, c = e.embed(["certificate expired", "certificates expiring", "disk usage high"])
    assert cosine(a, b) > cosine(a, c)


def test_ollama_embedder_batches_and_parses():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(len(body["input"]))
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * len(body["input"])})

    emb = OllamaEmbedder(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(emb.embed(["x"] * 130)) == 130 and seen == [64, 64, 2]
    assert make_embedder("ngram").name == "ngram" and make_embedder("ollama:foo").name == "ollama:foo"
    with pytest.raises(ValueError):
        make_embedder("nope")


def test_log_lines_with_the_same_template_become_one_document_with_a_count():
    data = ScenarioData(DATA / "bad_deploy-0")
    docs = build_docs(data)
    errors = [d for d in docs if d.kind == "log" and d.level == "ERROR" and "NullPointer" in d.text]
    assert len(errors) == 1 and errors[0].count > 20
    assert len(docs) < len(data.logs)


def test_parse_alert():
    assert parse_alert("ALERT [critical] checkout-service: error rate 24% since 2026-03-10T04:00:00Z") == (
        "checkout-service", "2026-03-10T04:00:00Z")
    assert parse_alert("something else") == ("", "")


def test_slate_has_the_deploy_that_landed_just_before_the_alert():
    data = ScenarioData(DATA / "bad_deploy-0")
    texts = [d.text for d in EvidenceRetriever(data).slate(data.alert, alert=data.alert)]
    assert any("v1.6.0" in t for t in texts) and any("NullPointerException" in t for t in texts)


def test_retrieval_never_reads_the_answer_file(tmp_path):
    import shutil
    shutil.copytree(DATA / "slow_query-0", tmp_path / "slow_query-0")
    (tmp_path / "slow_query-0" / "answer.json").unlink()
    data = ScenarioData(tmp_path / "slow_query-0")
    EvidenceRetriever(data).slate(data.alert, alert=data.alert)  # must not need it
    assert signature(data)


def test_an_incident_never_retrieves_itself():
    ids = ["bad_deploy-0", "bad_deploy-1", "slow_query-0"]
    kb = KnowledgeBase.build(DATA, ids)
    sig = signature(ScenarioData(DATA / "bad_deploy-0"))
    ranked = kb.rank(sig, exclude="bad_deploy-0", kinds=("history",))
    assert all("bad_deploy-0" not in h.support for h in ranked)
    assert ranked[0].category == "bad_deploy" and ranked[0].support[0] == "bad_deploy-1"


def test_rank_lists_every_category_even_without_votes():
    kb = KnowledgeBase.build(DATA, [], runbook=True)
    assert len(kb.rank("anything at all")) == 6


@pytest.mark.parametrize("sid", ["bad_deploy-2", "expired_certificate-3", "memory_leak-2"])
def test_runbook_ranks_the_true_category_first_on_heldout(sid):
    truth = json.loads((DATA / sid / "answer.json").read_text())["root_cause_category"]
    kb = KnowledgeBase.build(DATA, [])
    assert kb.rank(signature(ScenarioData(DATA / sid)))[0].category == truth


def test_toolbox_tools_work_and_exclude_the_current_incident():
    data = ScenarioData(DATA / "bad_deploy-0")
    box = RetrievalToolbox(data, DATA)
    out = box.call("find_evidence", {"query": "null pointer exception"})
    assert "NullPointerException" in out
    hyp = box.call("suggest_hypotheses", {})
    assert "bad_deploy (runbook match)" in hyp and "bad_deploy-0:" not in hyp
    assert "error" in box.call("find_evidence", {"query": "  "})
    assert "ERROR" in box.call("search_logs", {"query": "NullPointer"})  # normal tools still work


class _UsesRetrieval:
    """Calls the retrieval tools, then reports. A test double, not a model."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        from agent.llm import LLMResponse, ToolCall

        self.calls += 1
        names = [t["name"] for t in (tools or [])]
        assert "find_evidence" in names and "suggest_hypotheses" in names
        if self.calls == 1:
            return LLMResponse(tool_calls=[ToolCall("suggest_hypotheses", {})])
        if self.calls == 2:
            return LLMResponse(tool_calls=[ToolCall("find_evidence", {"query": "exception"})])
        return LLMResponse(tool_calls=[ToolCall("submit_report", {
            "root_cause_category": "bad_deploy", "component": "checkout-service", "confidence": "high",
            "summary": "s", "evidence": ["NullPointerException"], "action_type": "rollback_deploy",
            "action_target": "checkout-service"})])


def test_investigator_loop_runs_with_retrieval_tools_and_the_guard():
    from agent.investigator import run_investigation
    from guard.guard import Guard

    data = ScenarioData(DATA / "bad_deploy-0")
    for guard in (None, Guard()):
        r = run_investigation(_UsesRetrieval(), data, box=RetrievalToolbox(data, DATA), guard=guard)
        assert r.report and r.report["root_cause_category"] == "bad_deploy"
        used = [t.get("name") for t in r.trace if t["type"] == "tool_call"]
        assert "suggest_hypotheses" in used and "find_evidence" in used


def test_live_incident_with_a_simulated_id_keeps_the_simulated_incident_in_its_history(tmp_path):
    """Live incidents reuse ids like bad_deploy-0. Leaving one out by name would drop real history."""
    import shutil

    shutil.copytree(DATA / "bad_deploy-0", tmp_path / "bad_deploy-0")
    live = ScenarioData(tmp_path / "bad_deploy-0")
    assert "bad_deploy-0" in {e.id for e in RetrievalToolbox(live, DATA).kb.entries}  # different folder
    own = ScenarioData(DATA / "bad_deploy-0")
    assert "bad_deploy-0" not in {e.id for e in RetrievalToolbox(own, DATA).kb.entries}  # same folder


def test_retrieval_tools_sit_alongside_another_toolbox_s_tools():
    from gateway.tool import RUN_COMMAND_SPEC

    class Base:
        extra_specs = (RUN_COMMAND_SPEC,)

        def call(self, name, args):
            return f"base:{name}"

    box = RetrievalToolbox(ScenarioData(DATA / "bad_deploy-0"), DATA, base=Base())
    assert {t["name"] for t in box.extra_specs} == {"find_evidence", "suggest_hypotheses", "run_command"}
    assert box.call("run_command", {}) == "base:run_command"
    assert "NullPointer" in box.call("find_evidence", {"query": "null pointer"})
    assert len(RetrievalToolbox.extra_specs) == 2  # building one did not change the class default
