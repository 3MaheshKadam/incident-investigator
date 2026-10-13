"""Build one monitored episode: the shop runs for three hours, optionally with one fault.

An episode with no fault is a *healthy* run. It still has deploys, noise and a harmless traffic
surge, because a detector is only convincing if it also stays quiet when nothing is wrong.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from shop.faults import KINDS, Fault, make_fault, version
from shop.system import WINDOW_MIN, ShopSystem

POOLS = {
    "bad_deploy": ["checkout-service", "search-service", "auth-service", "notification-service"],
    "memory_leak": ["search-service", "auth-service", "notification-service", "checkout-service"],
    "expired_certificate": ["payments-gateway", "auth-service", "checkout-service", "search-service"],
    "slow_query": ["orders-db", "catalog-db", "customers-db", "orders-db"],
    "feature_flag": ["checkout-service", "search-service", "notification-service", "auth-service"],
    "dependency_outage": ["orders-db", "payments-gateway", "cache-proxy", "catalog-db"],
}
BENIGN_COMMITS = [
    "Update documentation link in health endpoint",
    "Bump logging library patch version",
    "Fix typo in error message",
    "Add unit tests for date parsing",
    "Tidy imports",
]
# One-off lines that look like other incident types. Real logs are full of these.
RED_HERRINGS = [
    ("ERROR", "container restarted: OOMKilled (exit code 137)"),
    ("WARN", "slow query (412ms): SELECT id FROM sessions WHERE token = ?; plan: Index Scan"),
    ("WARN", "upstream cache-proxy: connection refused (dial tcp 10.0.1.9:6379); retry succeeded"),
    ("WARN", "x509: certificate for internal-ca renewed (old certificate rotated out)"),
    ("ERROR", "timeout in experimental code path (call to model-v2 after 2000ms)"),
]
APPS = ["api-gateway", "auth-service", "search-service", "checkout-service", "notification-service"]


@dataclass
class Episode:
    id: str
    kind: str | None  # None means healthy
    variant: int
    system: ShopSystem
    fault: Fault | None

    @property
    def truth(self) -> dict | None:
        return self.fault.truth(self.system, self.id) if self.fault else None


def _schedule_deploy(system: ShopSystem, minute: int, service: str, ver: str, commit: str) -> None:
    system.at(minute, lambda sy, m: sy.deploy(m, service, ver, commit))


def _background(system: ShopSystem, rng: random.Random, culprit: str | None) -> None:
    """Things that happen in every healthy or unhealthy hour."""
    quiet = [s for s in APPS if s != culprit]
    for svc in rng.sample(APPS, 3):
        _schedule_deploy(system, rng.randint(5, 60), svc, version(rng), rng.choice(BENIGN_COMMITS))
    svc = rng.choice(APPS)
    system.at(rng.randint(5, 60), lambda sy, m, s=svc: sy.config(m, s, "log_level", "info", "info,debug"))
    for level, msg in rng.sample(RED_HERRINGS, 3):
        minute, where = rng.uniform(8, 70), rng.choice(quiet)
        system.log(minute, where, level, msg)


def _decoys(system: ShopSystem, rng: random.Random, fault: Fault) -> None:
    """Coincidences placed near the fault, so a recent change is not automatically the cause."""
    t0, k, svc = fault.at, fault.kind, fault.service
    other = rng.choice([s for s in APPS if s != svc])

    def benign(minute: int, who: str, commit: str) -> None:
        _schedule_deploy(system, max(minute, 1), who, version(rng), commit)

    if k == "bad_deploy":
        benign(t0 - 20, other, "Bump http client timeout to 5s")
        system.log(t0 - 12, svc, "WARN", "redis latency elevated (310ms)")
    elif k == "memory_leak":
        system.cpu_boost(svc, t0 + 60, t0 + 70, 78)
        benign(t0 + 30, svc, "Update documentation link in health endpoint")
    elif k == "expired_certificate":
        system.at(max(t0 - 60, 1), lambda sy, m: sy.config(m, svc, "connection_pool_size", "20", "40"))
        benign(t0 - 70, svc, "Bump logging library patch version")
    elif k == "slow_query":
        benign(t0 - 30, other, "Tidy imports")
        system.traffic_surge(t0 - 10, t0 + 20, 1.2)
    elif k == "feature_flag":
        system.at(max(t0 - 90, 1), lambda sy, m: sy.config(m, svc, "request_timeout_ms", "2000", "2000"))
        benign(t0 - 15, other, "Fix typo in error message")
    elif k == "dependency_outage":
        benign(t0 - 25, "api-gateway", "Bump http client timeout to 5s")


def build_episode(kind: str | None, variant: int, severity: float = 1.0) -> Episode:
    """Deterministic: the same (kind, variant, severity) always produces the same episode."""
    rng = random.Random(f"shop-{kind}-{variant}")
    system = ShopSystem(rng)
    sid = f"{kind}-{variant}" if kind else f"healthy-{variant}"
    fault = None
    if kind:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        t0 = rng.randint(100, 125)
        # A leak starts early, after the detector's baseline period, so it can reach the memory
        # limit before the window ends.
        at = rng.randint(35, 50) if kind == "memory_leak" else t0
        fault = make_fault(kind, POOLS[kind][variant % 4], at, rng, variant, severity)
        system.services[fault.service].severity = severity
        fault.inject(system)
        _decoys(system, rng, fault)
    else:
        # Hard negatives: a long traffic surge and a flurry of changes, but no fault.
        start = rng.randint(80, 110)
        system.traffic_surge(start, start + rng.randint(20, 40), rng.uniform(1.3, 1.6))
        for s in rng.sample(APPS, 2):
            _schedule_deploy(
                system, rng.randint(70, 120), s, version(rng), rng.choice(BENIGN_COMMITS)
            )
    _background(system, rng, fault.service if fault else None)
    system.run(WINDOW_MIN)
    return Episode(sid, kind, variant, system, fault)
