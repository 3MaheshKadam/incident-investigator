import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from monitor.record import downsample
from target.common import Metrics, parse_prometheus
from target.db import QUERY, ensure_db
from target.episode import LIVE_KINDS, LiveFault, Telemetry, make_live_fault
from target.stack import Stack

linux_only = pytest.mark.skipif(
    not Path("/proc/self/statm").exists(), reason="the live stack reads /proc"
)


def hit(url: str) -> int:
    try:
        return urllib.request.urlopen(url, timeout=3).status
    except urllib.error.HTTPError as e:
        return e.code


def test_prometheus_text_round_trips():
    m = Metrics()
    m.record(10.0, True)
    m.record(30.0, False)
    parsed = parse_prometheus(m.prometheus_text())
    assert parsed["service_error_rate"] == 50.0
    assert parsed["service_latency_p95_ms"] == 10.0  # sorted [10, 30], index int(0.95 * 1) = 0
    assert {"process_resident_memory_mb", "process_cpu_percent", "service_request_rate"} <= set(parsed)


def test_database_has_an_index_and_is_not_rebuilt(tmp_path):
    import sqlite3

    path = ensure_db(tmp_path / "t.sqlite", 2000)
    mtime = path.stat().st_mtime_ns
    assert ensure_db(path, 2000).stat().st_mtime_ns == mtime  # same size: reused
    plan = sqlite3.connect(path).execute("EXPLAIN QUERY PLAN " + QUERY, ("x",)).fetchall()
    assert "USING INDEX" in plan[0][3]


@linux_only
def test_real_stack_serves_traffic_reports_metrics_and_fails_for_real(tmp_path):
    stack = Stack(tmp_path / "run", db_rows=20_000)
    stack.start(db_path=tmp_path / "shop.sqlite")
    try:
        assert all(hit(stack.url("api-gateway") + "/checkout") == 200 for _ in range(20))
        gw = stack.scrape("api-gateway")
        assert gw["service_request_rate"] > 0 and gw["service_error_rate"] == 0
        assert 5 < gw["process_resident_memory_mb"] < 200  # a real process, measured by the kernel

        stack.kill("inventory-service")  # a real SIGKILL
        assert hit(stack.url("api-gateway") + "/checkout") == 500
        assert stack.scrape("inventory-service") is None  # nothing answers any more
        time.sleep(0.2)
        errors = [r["msg"] for r in stack.read_logs("api-gateway") if r["level"] == "ERROR"]
        assert any("upstream inventory-service: connection refused" in m for m in errors)
    finally:
        stack.stop()


@linux_only
def test_dropping_the_index_makes_the_query_really_slower(tmp_path):
    stack = Stack(tmp_path / "run", db_rows=300_000)
    stack.start(db_path=tmp_path / "shop.sqlite")
    try:
        def timed() -> float:
            t = time.perf_counter()
            hit(stack.url("orders-db") + "/query?email=user5@example.com")
            return time.perf_counter() - t

        before = min(timed() for _ in range(3))
        stack.ctl("orders-db", "/_ctl/drop_index")
        after = min(timed() for _ in range(3))
        assert after > 5 * before
    finally:
        stack.stop()


@linux_only
def test_releases_flags_and_events_are_recorded_for_real(tmp_path):
    stack = Stack(tmp_path / "run", db_rows=2000)
    stack.start(db_path=tmp_path / "shop.sqlite")
    try:
        stack.deploy("orders-service", "v9.9.9", "bad release", "li", BUG="1")
        statuses = [hit(stack.url("orders-service") + "/order") for _ in range(10)]
        assert 500 in statuses and 200 in statuses  # the bug hits some orders, not all
        stack.set_flag("fast_checkout_path", True, "orders-service", "li")
        assert stack.deploys[0][1]["version"] == "v9.9.9"
        assert stack.config_changes[0][1]["key"] == "fast_checkout_path"
        msgs = " ".join(r["msg"] for r in stack.read_logs("orders-service"))
        assert "AttributeError" in msgs and "v9.9.9" in msgs
    finally:
        stack.stop()


def test_live_faults_have_truth_in_the_scenario_format():
    import random

    for kind in LIVE_KINDS:
        fault = make_live_fault(kind, random.Random(kind))
        tele = Telemetry({}, [], [], [], first_oom={fault.service: 90})
        truth = fault.truth(tele, f"{kind}-0")
        assert {"id", "root_cause_category", "component", "evidence_keys", "incident_start",
                "correct_action"} <= set(truth)
        json.dumps(truth)
    assert isinstance(fault, LiveFault)
    assert downsample([1.0] * 10)[0][1] == 1.0


@pytest.mark.skipif(not os.environ.get("LIVE_TESTS"), reason="slow (about a minute); set LIVE_TESTS=1")
def test_investigator_uses_the_gateway_on_a_broken_live_stack(tmp_path):
    from agent.llm import LLMResponse, MockLLM, ToolCall
    from target.investigate import investigate_live

    report = {"root_cause_category": "dependency_outage", "component": "orders-service",
              "confidence": "medium", "summary": "down", "evidence": ["connection refused"],
              "action_type": "open_ticket", "action_target": "orders-service"}
    llm = MockLLM([
        LLMResponse(tool_calls=[ToolCall("run_command", {"command": "grep -c refused api-gateway.log"})]),
        LLMResponse(tool_calls=[ToolCall("run_command", {"command": "rm -rf /"})]),
        LLMResponse(tool_calls=[ToolCall("submit_report", report)]),
    ])
    row = investigate_live("dependency_outage", 0, llm, tmp_path, 3000, 100, 12, True)["row"]
    assert row["stop_reason"] == "report" and row["commands_run"] == 2 and row["commands_denied"] == 1
    assert int(row["audit"][0]["detail"]) > 0  # the gateway saw the real "connection refused" lines


def test_seed_is_repeatable_and_creates_flags(tmp_path):
    from target.seed import main

    assert main([str(tmp_path), "--rows", "500"]) == 0
    first = (tmp_path / "shop.sqlite").stat().st_mtime_ns
    (tmp_path / "flags.json").write_text('{"x": true}')
    assert main([str(tmp_path), "--rows", "500"]) == 0
    assert (tmp_path / "shop.sqlite").stat().st_mtime_ns == first  # not rebuilt
    assert json.loads((tmp_path / "flags.json").read_text()) == {"x": True}  # not reset


def _tiny_server(status: int = 200, delay: float = 0.0):
    import threading
    import time as _t
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from typing import ClassVar

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        seen_ports: ClassVar[set] = set()

        def do_GET(self):
            _t.sleep(delay)
            body = b'{"ok": 1}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            H.seen_ports.add(self.client_address[1])

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, H


def test_http_client_reuses_connections_and_keeps_urllib_errors():
    from target.httpc import get_json

    srv, handler = _tiny_server()
    url = f"http://127.0.0.1:{srv.server_address[1]}/x"
    try:
        for _ in range(20):
            assert get_json(url, 2.0) == (200, {"ok": 1})
        assert len(handler.seen_ports) == 1  # one TCP connection served all 20 calls
    finally:
        srv.shutdown()
        srv.server_close()
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    with pytest.raises(urllib.error.URLError) as e:  # nothing listening: same error urllib gives
        get_json(f"http://127.0.0.1:{closed_port}/x", 2.0)
    assert isinstance(e.value.reason, ConnectionRefusedError)


def test_http_client_maps_status_and_timeout_like_urllib():
    from target.httpc import get_json

    bad, _ = _tiny_server(status=500)
    slow, _ = _tiny_server(delay=0.5)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get_json(f"http://127.0.0.1:{bad.server_address[1]}/x", 2.0)
        assert e.value.code == 500
        with pytest.raises(TimeoutError):
            get_json(f"http://127.0.0.1:{slow.server_address[1]}/x", 0.1)
    finally:
        for s in (bad, slow):
            s.shutdown()
            s.server_close()
