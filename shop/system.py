"""A small simulated shop backend: services that call each other, with real state.

Telemetry (logs, metrics, deploys, config changes) is *derived from that state* every simulated
minute instead of being written by hand per incident. Injecting a fault (see faults.py) changes
state, and the symptoms follow: a down database makes its callers fail, a slow query makes them
slow, a leak fills memory until the process is killed, and so on.

Nothing here is a model of reality. It is a deliberately small system whose behaviour is simple
enough to explain, but whose symptoms propagate through dependencies the way real ones do.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

START = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)
WINDOW_MIN = 180
METRICS = ("error_rate", "latency_p95_ms", "memory_mb", "cpu_pct", "request_rate")
AUTHORS = ["priya", "tom", "li", "carlos", "dana"]
TIMEOUT_MS = 3100

INFO_LINES = [
    "GET /health 200 2ms",
    "cache hit ratio 0.93",
    "request completed in 48ms",
    "background job finished (processed 120 items)",
    "connection pool: 6/20 in use",
]
WARN_LINES = [
    "slow response from cache (85ms)",
    "retrying connection to redis (attempt 1)",
    "GC pause 120ms",
    "request queue depth 14",
]


def ts(minute: float) -> str:
    return (START + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Service:
    name: str
    deps: dict[str, float] = field(default_factory=dict)  # dependency -> share of calls to it
    base_rps: float = 200.0
    base_latency: float = 120.0
    base_mem: float = 800.0
    mem_limit: float = 2048.0
    version: str = "v1.0.0"
    # Fault state. Only injected faults change these.
    bug_rate: float = 0.0  # share of requests failing because of a bad release
    leak_mb_per_min: float = 0.0
    cert_expired_at: int | None = None
    slow_table: str | None = None
    flag_err_rate: float = 0.0
    down_reason: str = ""  # non-empty means the service is down
    severity: float = 1.0  # how much of the traffic a fault hits (1.0 = all of it)
    # Live state
    mem_mb: float = 0.0
    heap_warned: set[int] = field(default_factory=set)
    # Computed every minute
    error: float = 0.0
    latency: float = 0.0
    rps: float = 0.0
    cpu: float = 0.0
    causes: set[str] = field(default_factory=set)


def default_services() -> dict[str, Service]:
    svcs = [
        Service("cache-proxy", base_rps=600, base_latency=4, base_mem=900),
        Service("orders-db", base_rps=900, base_latency=15, base_mem=4000, mem_limit=8192),
        Service("catalog-db", base_rps=700, base_latency=12, base_mem=3800, mem_limit=8192),
        Service("customers-db", base_rps=500, base_latency=14, base_mem=3600, mem_limit=8192),
        Service("payments-gateway", base_rps=120, base_latency=180, base_mem=700),
        Service("notification-service", base_rps=150, base_latency=90, base_mem=600),
        Service("auth-service", {"cache-proxy": 0.5, "customers-db": 0.4}, 300, 40, 650),
        Service("search-service", {"cache-proxy": 0.7, "catalog-db": 0.5}, 250, 110, 800),
        Service(
            "checkout-service",
            {"orders-db": 0.8, "payments-gateway": 0.5, "cache-proxy": 0.6},
            180, 150, 820,
        ),
        Service(
            "api-gateway",
            {"checkout-service": 0.4, "search-service": 0.3, "auth-service": 0.3,
             "notification-service": 0.1},
            400, 60, 700,
        ),
    ]
    for s in svcs:
        s.mem_mb = s.base_mem
    return {s.name: s for s in svcs}


# Callees before callers, so a service's dependencies are always computed first.
ORDER = [
    "cache-proxy", "orders-db", "catalog-db", "customers-db", "payments-gateway",
    "notification-service", "auth-service", "search-service", "checkout-service", "api-gateway",
]
Event = Callable[["ShopSystem", int], None]


class ShopSystem:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.services = default_services()
        self.logs: list[tuple[float, str, str, str]] = []  # (minute, service, level, message)
        self.metrics: dict[str, dict[str, list[float]]] = {
            n: {m: [] for m in METRICS} for n in self.services
        }
        self.deploys: list[dict] = []
        self.config_changes: list[dict] = []
        self.first_oom: dict[str, int] = {}
        self._events: dict[int, list[Event]] = {}
        self._surges: list[tuple[int, int, float]] = []
        self._cpu_boosts: list[tuple[str, int, int, float]] = []

    # ----- things that happen to the system ----------------------------------------------------
    def at(self, minute: int, fn: Event) -> None:
        self._events.setdefault(minute, []).append(fn)

    def log(self, minute: float, service: str, level: str, msg: str) -> None:
        self.logs.append((minute, service, level, msg))

    def deploy(self, minute: int, service: str, version: str, commit: str) -> None:
        self.services[service].version = version
        self.deploys.append(
            {"ts": ts(minute), "service": service, "version": version,
             "author": self.rng.choice(AUTHORS), "commit_message": commit}
        )

    def config(self, minute: int, service: str, key: str, old: str, new: str) -> None:
        self.config_changes.append(
            {"ts": ts(minute), "service": service, "key": key, "old": old, "new": new,
             "author": self.rng.choice(AUTHORS)}
        )

    def traffic_surge(self, start: int, end: int, factor: float) -> None:
        """Harmless extra load. A good detector must not call this an incident."""
        self._surges.append((start, end, factor))

    def cpu_boost(self, service: str, start: int, end: int, value: float) -> None:
        self._cpu_boosts.append((service, start, end, value))

    # ----- the simulation ----------------------------------------------------------------------
    def run(self, minutes: int = WINDOW_MIN) -> None:
        for m in range(minutes):
            for fn in self._events.get(m, []):
                fn(self, m)
            for name in ORDER:
                s = self.services[name]
                self._compute(s, m)
                self._emit_logs(s, m)
                self._record(s, m)

    def _surge(self, m: int) -> float:
        f = 1.0
        for a, b, k in self._surges:
            if a <= m < b:
                f *= k
        return f

    def _compute(self, s: Service, m: int) -> None:
        rng = self.rng
        surge = self._surge(m)
        s.causes = set()
        rps = rng.gauss(s.base_rps, s.base_rps * 0.05) * surge
        err = max(0.0, rng.gauss(0.005, 0.0015))
        lat = rng.gauss(s.base_latency, max(1.0, s.base_latency * 0.08)) * (1 + 0.15 * (surge - 1))
        cpu = rng.gauss(35, 4) * (1 + 0.8 * (surge - 1))
        for name, a, b, v in self._cpu_boosts:
            if name == s.name and a <= m <= b:
                cpu = rng.gauss(v, 3)

        sev = s.severity
        if s.down_reason:
            err = rng.uniform(0.98, 1.0) * sev
            rps = rps * (1 - sev) + rng.uniform(0, 3) * sev
            s.causes.add("down")
        if s.cert_expired_at is not None and m >= s.cert_expired_at:
            err = rng.uniform(0.95, 0.99) * sev
            rps = rps * (1 - sev) + rng.gauss(60, 5) * sev
            s.causes.add("cert")
        if s.bug_rate:
            err += s.bug_rate + rng.gauss(0, 0.01)
            lat += 140 + rng.gauss(0, 20)
            s.causes.add("bug")
        if s.flag_err_rate:
            err += s.flag_err_rate + rng.gauss(0, 0.01)
            lat = rng.gauss(2100, 100)
            s.causes.add("flag")
        if s.slow_table:
            lat += (rng.gauss(2800, 150) - lat) * sev
            cpu = rng.gauss(90, 3)
            s.causes.add("slow_query")
        if s.leak_mb_per_min:
            s.mem_mb += s.leak_mb_per_min
            if s.mem_mb >= s.mem_limit:  # the container is killed and restarted
                s.mem_mb = s.base_mem * 0.75
                s.heap_warned.clear()
                # After a restart queued requests are replayed, so the leak fills faster.
                s.leak_mb_per_min = min(s.leak_mb_per_min * 2.5, 64.0)
                err += rng.uniform(0.02, 0.08)
                s.causes.add("oom")
                self.first_oom.setdefault(s.name, m)

        # Failures and slowness travel up the call chain.
        for dep_name, share in s.deps.items():
            dep = self.services[dep_name]
            err += share * max(0.0, dep.error - 0.01)
            lat += share * max(0.0, dep.latency - dep.base_latency)
        s.error = min(1.0, err)
        s.latency = min(lat, TIMEOUT_MS + rng.uniform(0, 80))
        s.rps, s.cpu = rps, cpu

    def _worst_dep(self, s: Service) -> Service | None:
        bad = [(self.services[d].error, self.services[d]) for d in s.deps]
        bad = [b for b in bad if b[0] > 0.03]
        return max(bad, key=lambda b: b[0])[1] if bad else None

    def _emit_logs(self, s: Service, m: int) -> None:
        rng = self.rng
        if rng.random() < 0.14:
            self.log(m + rng.random(), s.name, "INFO", rng.choice(INFO_LINES))
        if rng.random() < 0.022:
            self.log(m + rng.random(), s.name, "WARN", rng.choice(WARN_LINES))

        if "slow_query" in s.causes and rng.random() < 0.5:
            self.log(
                m + rng.random(), s.name, "WARN",
                f"slow query ({rng.randint(2500, 4800)}ms): SELECT * FROM {s.slow_table} "
                f"WHERE customer_email LIKE '%@example.com'; plan: Seq Scan on {s.slow_table}",
            )
        if any("slow_query" in self.services[d].causes for d in s.deps) and rng.random() < 0.3:
            self.log(m + rng.random(), s.name, "WARN", "request timed out waiting for database (3000ms)")

        if s.leak_mb_per_min:
            for pct in (85, 93):
                if pct not in s.heap_warned and s.mem_mb >= s.mem_limit * pct / 100:
                    s.heap_warned.add(pct)
                    self.log(
                        m + rng.random(), s.name, "WARN",
                        f"heap usage {pct}% of limit ({s.mem_mb:.0f}MB/{s.mem_limit:.0f}MB)",
                    )
        if "oom" in s.causes:
            self.log(m + rng.random(), s.name, "ERROR", "container restarted: OOMKilled (exit code 137)")

        if s.error > 0.03 and s.causes - {"oom"}:
            for _ in range(1 + int(rng.random() < 0.6)):
                self.log(m + rng.random(), s.name, "ERROR", self._error_message(s, m))
        elif s.error > 0.03 and not s.causes:
            dep = self._worst_dep(s)
            if dep:
                for _ in range(1 + int(rng.random() < 0.6)):
                    self.log(m + rng.random(), s.name, "ERROR", self._upstream_message(dep))

    def _error_message(self, s: Service, m: int) -> str:
        if "cert" in s.causes:
            return (
                "tls handshake failed: x509: certificate has expired or is not yet valid: "
                f"current time {ts(m)} is after {ts(s.cert_expired_at or 0)}"
            )
        if "down" in s.causes:
            return f"health check failing: {s.down_reason}"
        if "bug" in s.causes:
            cls = "".join(p.title() for p in s.name.split("-")) + "Handler"
            return f"NullPointerException: field 'currency' is null in {cls}.handle"
        if "flag" in s.causes:
            return "timeout in experimental code path (call to model-v2 after 2000ms)"
        dep = self._worst_dep(s)
        return self._upstream_message(dep) if dep else "request failed: 503"

    @staticmethod
    def _upstream_message(dep: Service) -> str:
        if dep.down_reason:
            return f"upstream {dep.name}: connection refused (dial tcp 10.0.3.14:8443)"
        return f"upstream {dep.name}: returned 5xx"

    def _record(self, s: Service, m: int) -> None:
        rng = self.rng
        series = self.metrics[s.name]
        series["error_rate"].append(round(s.error * 100, 2))
        series["latency_p95_ms"].append(round(s.latency, 2))
        series["memory_mb"].append(round(s.mem_mb + rng.gauss(0, 12), 2))
        series["cpu_pct"].append(round(s.cpu, 2))
        series["request_rate"].append(round(s.rps, 2))
