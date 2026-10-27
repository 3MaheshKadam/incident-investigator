"""Run the real stack for a short time with one real fault, and collect what it reported.

Time is compressed: one sample every SAMPLE_S real seconds is treated as one "minute" by the
detector and in recorded timestamps. A 150-sample episode takes about 75 real seconds.
"""

from __future__ import annotations

import random
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from monitor.detect import DetectorConfig, Rule
from shop.system import METRICS, ts
from target.load import Load
from target.stack import SERVICES, Stack

SAMPLE_S = 0.5
SAMPLES = 150
LIVE_KINDS = ["bad_deploy", "memory_leak", "slow_query", "feature_flag", "dependency_outage"]
AUTHORS = ["priya", "tom", "li", "carlos", "dana"]
BENIGN_COMMITS = ["Update documentation link in health endpoint", "Bump logging library patch version",
                  "Fix typo in error message", "Add unit tests for date parsing", "Tidy imports"]
RED_HERRINGS = [
    ("ERROR", "container restarted: OOMKilled (exit code 137)"),
    ("WARN", "slow query (412ms): SELECT id FROM sessions WHERE token = ?; plan: SEARCH sessions USING INDEX"),
    ("WARN", "x509: certificate for internal-ca renewed (old certificate rotated out)"),
    ("ERROR", "timeout in experimental code path (call to model-v2 after 2000ms)"),
]
# A small real app is measured in milliseconds and tens of megabytes, not the simulated shop's scale.
LIVE_CONFIG = DetectorConfig(
    rules={
        "error_rate": Rule(min_delta=3.0, scale_floor=0.5, threshold=5.0),
        "latency_p95_ms": Rule(min_delta=40.0, scale_floor=2.0, min_ratio=2.0, threshold=800.0),
        "memory_mb": Rule(min_delta=30.0, scale_floor=2.0, threshold=150.0),
    },
    train=30,
    consecutive=4,
)


@dataclass
class Telemetry:
    """The same shape as shop.ShopSystem's telemetry, so the monitor and recorder need no changes."""

    metrics: dict[str, dict[str, list[float]]]
    logs: list[tuple[float, str, str, str]]
    deploys: list[dict]
    config_changes: list[dict]
    first_oom: dict[str, int] = field(default_factory=dict)


@dataclass
class LiveFault:
    kind: str
    service: str
    at: int
    params: dict

    def impact_start(self, system: Telemetry) -> int | None:
        if self.kind == "memory_leak":
            return system.first_oom.get(self.service)
        return self.at + (1 if self.kind in ("bad_deploy", "feature_flag") else 0)

    def truth(self, system: Telemetry, sid: str) -> dict:
        p = self.params
        evidence = {
            "bad_deploy": [p.get("version"), "AttributeError"],
            "memory_leak": ["OOMKilled"],
            "slow_query": ["slow query", "SCAN orders"],
            "feature_flag": ["fast_checkout_path"],
            "dependency_outage": ["connection refused"],
        }[self.kind]
        action = {
            "bad_deploy": ("rollback_deploy", self.service),
            "memory_leak": ("restart_service", self.service),
            "slow_query": ("open_ticket", self.service),
            "feature_flag": ("toggle_feature_flag", "fast_checkout_path"),
            "dependency_outage": ("open_ticket", self.service),
        }[self.kind]
        start = self.impact_start(system)
        return {
            "id": sid, "kind": self.kind, "root_cause_category": self.kind,
            "component": self.service, "evidence_keys": evidence,
            "incident_start": ts(start) if start is not None else None,
            "injected_at": ts(self.at),
            "correct_action": {"type": action[0], "target": action[1]},
        }


@dataclass
class LiveEpisode:
    id: str
    kind: str | None
    variant: int
    system: Telemetry
    fault: LiveFault | None

    @property
    def truth(self) -> dict | None:
        return self.fault.truth(self.system, self.id) if self.fault else None


def _inject(stack: Stack, fault: LiveFault, rng: random.Random) -> None:
    k, p = fault.kind, fault.params
    if k == "bad_deploy":
        stack.deploy(fault.service, p["version"], "Refactor request handling and add currency conversion",
                     rng.choice(AUTHORS), BUG="1")
    elif k == "memory_leak":
        stack.ctl(fault.service, f"/_ctl/leak?mb_per_s={p['mb_per_s']}")
    elif k == "slow_query":
        stack.ctl(fault.service, "/_ctl/drop_index")
    elif k == "feature_flag":
        stack.set_flag("fast_checkout_path", True, fault.service, rng.choice(AUTHORS))
    elif k == "dependency_outage":
        stack.kill(fault.service)


def make_live_fault(kind: str, rng: random.Random) -> LiveFault:
    service = {"bad_deploy": "orders-service", "memory_leak": "inventory-service",
               "slow_query": "orders-db", "feature_flag": "orders-service",
               "dependency_outage": "inventory-service"}[kind]
    at = rng.randint(35, 45) if kind == "memory_leak" else rng.randint(45, 60)
    params = {}
    if kind == "bad_deploy":
        params["version"] = f"v{rng.randint(1, 4)}.{rng.randint(1, 20)}.{rng.randint(0, 9)}"
    if kind == "memory_leak":
        params["mb_per_s"] = round(rng.uniform(2.5, 3.5), 1)
    return LiveFault(kind, service, at, params)


def run_live_episode(
    kind: str | None, variant: int, workdir: Path, db_rows: int = 1_000_000,
    samples: int = SAMPLES, interval: float = SAMPLE_S,
    inspect: Callable[[Stack, LiveEpisode], None] | None = None,
    load_threads: int = 4,
    surge_at: int | None = None,
    surge_threads: int = 24,
) -> LiveEpisode:
    if kind is not None and kind not in LIVE_KINDS:
        raise ValueError(f"{kind!r} is not available on the live stack ({LIVE_KINDS})")
    rng = random.Random(f"live-{kind}-{variant}")
    sid = f"{kind}-{variant}" if kind else f"healthy-{variant}"
    fault = make_live_fault(kind, rng) if kind else None

    stack = Stack(Path(workdir) / sid, db_rows=db_rows)

    # Things that happen in any hour: harmless releases, a config tweak, odd one-off log lines.
    # Each action is bound to its arguments now, so the random draws do not depend on timing.
    schedule: dict[int, list] = {}

    def later(sample: int, fn, *args) -> None:
        schedule.setdefault(sample, []).append(partial(fn, *args))

    apps = [s for s in SERVICES if s != "orders-db"]
    for svc in rng.sample(apps, 3):
        version = f"v{rng.randint(1, 4)}.{rng.randint(1, 20)}.{rng.randint(0, 9)}"
        later(rng.randint(3, 25), stack.record_deploy, svc, version,
              rng.choice(BENIGN_COMMITS), rng.choice(AUTHORS))
    later(rng.randint(3, 25), stack.record_config, rng.choice(apps), "log_level", "info",
          "info,debug", rng.choice(AUTHORS))
    quiet = [s for s in apps if not fault or s != fault.service]
    for level, msg in rng.sample(RED_HERRINGS, 3):
        later(rng.randint(3, 25), stack.ctl, rng.choice(quiet),
              f"/_ctl/log?level={level}&msg={urllib.request.quote(msg)}")
    if fault:
        later(fault.at, _inject, stack, fault, rng)

    stack.start(db_path=Path(workdir) / f"shop-{db_rows}.sqlite")
    load = Load(stack.url("api-gateway"), load_threads)
    surge = Load(stack.url("api-gateway"), surge_threads)  # extra users arriving mid-run
    if surge_at is not None:
        later(surge_at, surge.start)
    metrics = {n: {m: [] for m in METRICS} for n in SERVICES}
    last_mem = {n: 0.0 for n in SERVICES}
    try:
        load.start()
        t0 = time.time() + 0.5
        for i in range(samples):
            time.sleep(max(0.0, t0 + i * interval - time.time()))
            stack.tick()
            for fn in schedule.get(i, []):
                fn()
            for name in SERVICES:
                snap = stack.scrape(name)
                if snap is None:  # not answering: what an "up == 0" alert would see
                    values = (100.0, 0.0, last_mem[name], 0.0, 0.0)
                else:
                    last_mem[name] = snap["process_resident_memory_mb"]
                    values = (snap["service_error_rate"], snap["service_latency_p95_ms"],
                              snap["process_resident_memory_mb"], snap["process_cpu_percent"],
                              snap["service_request_rate"])
                for metric, v in zip(METRICS, values, strict=True):
                    metrics[name][metric].append(round(v, 2))
        load.stop()
        surge.stop()

        def minute(t: float) -> float:
            return (t - t0) / interval

        logs = [
            (minute(rec["ts"]), name, rec["level"], rec["msg"])
            for name in SERVICES for rec in stack.read_logs(name) if minute(rec["ts"]) >= 0
        ]
        telemetry = Telemetry(
            metrics=metrics,
            logs=logs,
            deploys=[{**d, "ts": ts(max(0, minute(t)))} for t, d in stack.deploys],
            config_changes=[{**c, "ts": ts(max(0, minute(t)))} for t, c in stack.config_changes],
            first_oom={n: int(minute(t)) for t, n in reversed(stack.oom_events)},
        )
        episode = LiveEpisode(sid, kind, variant, telemetry, fault)
        if inspect is not None:
            inspect(stack, episode)  # the stack is still running, with the fault in place
        return episode
    finally:
        load.stop()
        surge.stop()
        stack.stop()

