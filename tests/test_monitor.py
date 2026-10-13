import json
from pathlib import Path

import pytest

from agent.data import ScenarioData
from evals.detect_eval import evaluate_split
from monitor.detect import detect_incidents
from monitor.record import downsample, record_all
from shop.episode import build_episode
from shop.faults import KINDS
from shop.system import ts

RECORDED = Path(__file__).resolve().parent.parent / "scenarios" / "recorded"


def _errors(ep, service, text):
    return [m for m, s, lv, msg in ep.system.logs if s == service and lv == "ERROR" and text in msg]


def test_episodes_are_deterministic():
    a, b = build_episode("bad_deploy", 1), build_episode("bad_deploy", 1)
    assert a.system.logs == b.system.logs and a.system.metrics == b.system.metrics
    assert build_episode("bad_deploy", 1).system.logs != build_episode("bad_deploy", 2).system.logs


def test_symptoms_come_from_state_not_scripts():
    ep = build_episode("bad_deploy", 0)
    impact = ep.fault.impact_start(ep.system)
    hits = _errors(ep, ep.fault.service, "NullPointerException")
    assert hits and min(hits) >= impact  # no symptom before the bug shipped

    dep = build_episode("dependency_outage", 0)
    down = dep.fault.service
    assert _errors(dep, down, "health check failing")
    # the failure travels up the call chain: the caller of the down service logs it
    callers = [n for n, sv in dep.system.services.items() if down in sv.deps]
    assert any(_errors(dep, c, f"upstream {down}: connection refused") for c in callers)
    assert dep.system.metrics["api-gateway"]["error_rate"][-1] > 5  # and reaches users

    slow = build_episode("slow_query", 0)
    assert any("Seq Scan" in m for _, s, _, m in slow.system.logs if s == slow.fault.service)
    # a slow database slows its callers, even though the callers log no errors about it
    assert max(slow.system.metrics["checkout-service"]["latency_p95_ms"]) > 1000

    leak = build_episode("memory_leak", 0)
    mem = leak.system.metrics[leak.fault.service]["memory_mb"]
    assert max(mem) > 1900 and leak.system.first_oom  # climbs, then the container is killed


def test_every_fault_is_detected_and_healthy_runs_are_quiet():
    for kind in KINDS:
        ep = build_episode(kind, 0)
        s = ep.system
        incidents = detect_incidents(s.metrics, s.deploys, s.config_changes)
        assert incidents, kind
        impact = ep.fault.impact_start(s)
        assert incidents[0].detected_minute - impact <= 5  # negative for leaks: early warning
    for v in range(4):
        s = build_episode(None, v).system
        assert detect_incidents(s.metrics, s.deploys, s.config_changes) == []


def test_alert_pages_the_user_facing_service_and_links_the_change():
    ep = build_episode("bad_deploy", 0)
    s = ep.system
    inc = detect_incidents(s.metrics, s.deploys, s.config_changes)[0]
    assert inc.symptom_service == "api-gateway" and "normally" in inc.alert
    assert any(c["service"] == ep.fault.service and c["kind"] == "deploy" for c in inc.suspects)


def test_a_quieter_fault_is_harder_to_detect():
    loud = evaluate_split("dev", 1.0)
    quiet = evaluate_split("dev", 0.1)
    assert loud["recall"] == 1.0 and quiet["recall"] < loud["recall"]


def test_downsample_averages_blocks():
    assert downsample([1, 2, 3, 4, 5, 10], 5) == [[ts(0), 3.0], [ts(5), 10.0]]


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    out = tmp_path_factory.mktemp("recorded")
    record_all(out)
    return out


def test_recorded_scenarios_load_and_hide_the_answer(recorded):
    manifest = json.loads((recorded / "manifest.json").read_text())["scenarios"]
    assert len(manifest) == 24 and {e["split"] for e in manifest} == {"dev", "heldout"}
    data = ScenarioData(recorded / "slow_query-0")
    assert data.alert.startswith("ALERT") and not hasattr(data, "answer")
    blob = json.dumps([data.alert, data.logs, data.metrics, data.deploys, data.config_changes])
    assert "correct_action" not in blob and "evidence_keys" not in blob


def test_committed_recorded_data_matches_generator(recorded):
    for name in ("alert.txt", "answer.json", "deploys.json", "logs.jsonl"):
        assert (RECORDED / "memory_leak-2" / name).read_text() == (
            recorded / "memory_leak-2" / name
        ).read_text()
