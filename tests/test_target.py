import json
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
