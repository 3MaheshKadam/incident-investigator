"""Deterministic generator for simulated production incidents.

Each scenario is a folder holding generated logs, metric series, a deploy history, config
changes, the alert text, and a hidden `answer.json` naming the true root cause. Every scenario
has background noise and at least one misleading clue.

Run:  python -m scenarios.generate
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)
WINDOW_MIN = 180
STEP_MIN = 5
N_POINTS = WINDOW_MIN // STEP_MIN
VARIANTS_PER_KIND = 4
DEV_VARIANTS = {0, 1}  # variants 2 and 3 are held out for final numbers

CATEGORIES = [
    "bad_deploy",
    "memory_leak",
    "expired_certificate",
    "slow_query",
    "feature_flag",
    "dependency_outage",
]
ACTIONS = ["rollback_deploy", "restart_service", "toggle_feature_flag", "open_ticket"]

NOISE_SERVICES = ["api-gateway", "auth-service", "notification-service", "cache-proxy"]
AUTHORS = ["priya", "tom", "li", "carlos", "dana"]
BENIGN_COMMITS = [
    "Update documentation link in health endpoint",
    "Bump logging library patch version",
    "Fix typo in error message",
    "Add unit tests for date parsing",
    "Tidy imports",
]
# Realistic one-off lines that look like other incident types. Real logs are full of these, so
# a good investigator has to weigh timing, rate and service rather than match a keyword.
RED_HERRINGS = [
    ("ERROR", "container restarted: OOMKilled (exit code 137)"),
    ("WARN", "slow query (412ms): SELECT id FROM sessions WHERE token = ?; plan: Index Scan"),
    ("WARN", "upstream cache-proxy: connection refused (dial tcp 10.0.1.9:6379); retry succeeded"),
    ("WARN", "x509: certificate for internal-ca renewed (old certificate rotated out)"),
    ("ERROR", "timeout in experimental code path (call to model-v2 after 2000ms)"),
]
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


def _version(rng: random.Random) -> str:
    return f"v{rng.randint(1, 4)}.{rng.randint(1, 20)}.{rng.randint(0, 9)}"


class Builder:
    def __init__(self, rng: random.Random, services: list[str]):
        self.rng = rng
        self.services = services
        self.t0 = rng.randint(110, 130)  # when the incident really starts (minute)
        self.logs: list[tuple[float, str, str, str]] = []
        self.deploys: list[dict] = []
        self.config_changes: list[dict] = []
        self.m: dict[str, dict[str, list[float]]] = {}
        for s in services:
            self.m[s] = {
                "error_rate": [max(0.0, rng.gauss(0.5, 0.15)) for _ in range(N_POINTS)],
                "latency_p95_ms": [rng.gauss(120, 10) for _ in range(N_POINTS)],
                "memory_mb": [rng.gauss(800, 15) for _ in range(N_POINTS)],
                "cpu_pct": [rng.gauss(35, 4) for _ in range(N_POINTS)],
                "request_rate": [rng.gauss(200, 10) for _ in range(N_POINTS)],
            }
        self._add_noise()

    # ----- helpers -------------------------------------------------------------------------
    def log(self, minute: float, service: str, level: str, msg: str) -> None:
        self.logs.append((minute, service, level, msg))

    def deploy(self, minute: float, service: str, version: str, commit: str) -> None:
        self.deploys.append(
            {
                "ts": ts(minute),
                "service": service,
                "version": version,
                "author": self.rng.choice(AUTHORS),
                "commit_message": commit,
            }
        )

    def config(self, minute: float, service: str, key: str, old: str, new: str) -> None:
        self.config_changes.append(
            {
                "ts": ts(minute),
                "service": service,
                "key": key,
                "old": old,
                "new": new,
                "author": self.rng.choice(AUTHORS),
            }
        )

    def shape(self, service: str, metric: str, start: float, end: float, fn) -> None:
        """Overwrite a metric between two minutes using fn(minute) -> value."""
        series = self.m[service][metric]
        for i in range(N_POINTS):
            minute = i * STEP_MIN
            if start <= minute <= end:
                series[i] = fn(minute)

    def _add_noise(self) -> None:
        rng = self.rng
        for s in self.services:
            for _ in range(25):
                self.log(rng.uniform(0, WINDOW_MIN), s, "INFO", rng.choice(INFO_LINES))
            for _ in range(4):
                self.log(rng.uniform(0, WINDOW_MIN - 20), s, "WARN", rng.choice(WARN_LINES))
        quiet = self.services[1:]  # services[0] is the culprit; keep red herrings elsewhere
        for level, msg in rng.sample(RED_HERRINGS, 3):
            self.log(rng.uniform(8, 70), rng.choice(quiet), level, msg)
        for s in rng.sample(self.services, 3):
            self.deploy(rng.randint(5, 60), s, _version(rng), rng.choice(BENIGN_COMMITS))
        self.config(
            rng.randint(5, 60), rng.choice(self.services), "log_level", "info", "info,debug"
        )

    def write(self, path: Path, alert: str, answer: dict) -> None:
        path.mkdir(parents=True, exist_ok=True)
        logs = sorted(self.logs, key=lambda x: x[0])
        with open(path / "logs.jsonl", "w") as f:
            f.writelines(json.dumps({"ts": ts(minute), "service": service, "level": level, "msg": msg})
                    + "\n" for minute, service, level, msg in logs)
        metrics = {
            s: {
                name: [[ts(i * STEP_MIN), round(v, 2)] for i, v in enumerate(vals)]
                for name, vals in series.items()
            }
            for s, series in self.m.items()
        }
        _dump(path / "metrics.json", metrics)
        _dump(path / "deploys.json", sorted(self.deploys, key=lambda d: d["ts"]))
        _dump(path / "config_changes.json", sorted(self.config_changes, key=lambda d: d["ts"]))
        (path / "alert.txt").write_text(alert + "\n")
        _dump(path / "answer.json", answer)  # hidden: never read by agent tools


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1) + "\n")


def _answer(sid, kind, component, keys, t0, action, target) -> dict:
    return {
        "id": sid,
        "kind": kind,
        "root_cause_category": kind,
        "component": component,
        "evidence_keys": keys,
        "incident_start": ts(t0),
        "correct_action": {"type": action, "target": target},
    }


# ----- incident builders ---------------------------------------------------------------------
POOLS = {
    "bad_deploy": ["checkout-service", "cart-service", "pricing-service", "profile-service"],
    "memory_leak": ["search-service", "image-resizer", "session-service", "report-service"],
    "expired_certificate": ["payments-gateway", "login-service", "partner-api", "webhook-service"],
    "slow_query": ["orders-service", "customer-service", "billing-service", "reporting-service"],
    "feature_flag": ["recommendation-service", "search-ranker", "feed-service", "offers-service"],
    "dependency_outage": ["inventory-service", "shipping-service", "tax-service", "pricing-api"],
}
FLAGS = ["new_ranking_model", "fast_checkout_path", "recs_v2_pipeline", "async_feed_render"]


def build_bad_deploy(b: Builder, svc: str, variant: int):
    t0, rng = b.t0, b.rng
    ver = _version(rng)
    cls = "".join(p.title() for p in svc.split("-")) + "Handler"
    b.deploy(t0 - 1, svc, ver, "Refactor request handling and add currency conversion")
    other = rng.choice([s for s in NOISE_SERVICES if s != svc])
    b.deploy(t0 - 20, other, _version(rng), "Bump http client timeout to 5s")  # misleading
    b.log(t0 - 12, svc, "WARN", "redis latency elevated (310ms)")  # misleading
    err = rng.uniform(10, 25)
    b.shape(svc, "error_rate", t0, WINDOW_MIN, lambda m: err + rng.gauss(0, 1))
    b.shape(svc, "latency_p95_ms", t0, WINDOW_MIN, lambda m: rng.gauss(260, 20))
    m = t0 + 1
    while m < t0 + 60:
        b.log(m, svc, "ERROR", f"NullPointerException: field 'currency' is null in {cls}.handle")
        m += rng.uniform(1, 3)
    alert = f"ALERT [critical] {svc}: error rate {err:.0f}% (threshold 5%) since {ts(t0 + 3)}"
    ans = _answer("", "bad_deploy", svc, [ver, "NullPointerException"], t0, "rollback_deploy", svc)
    return alert, ans


def build_memory_leak(b: Builder, svc: str, variant: int):
    t0, rng = b.t0, b.rng
    start = t0 - 90
    slope = (2048 - 600) / 90

    def mem(m):
        if m < t0:
            return 600 + (m - start) * slope + rng.gauss(0, 8)
        return 600 + ((m - t0) % 15) * (1448 / 15) * 1.0 + rng.gauss(0, 8)

    b.shape(svc, "memory_mb", start, WINDOW_MIN, mem)
    b.shape(svc, "error_rate", t0, WINDOW_MIN, lambda m: rng.uniform(2, 8))
    b.shape(svc, "cpu_pct", t0 - 30, t0 - 20, lambda m: rng.gauss(78, 3))  # misleading
    b.deploy(t0 - 60, svc, _version(rng), "Update documentation link in health endpoint")  # mislead
    b.log(t0 - 25, svc, "WARN", "heap usage 85% of limit (1741MB/2048MB)")
    b.log(t0 - 8, svc, "WARN", "heap usage 93% of limit (1905MB/2048MB)")
    m = t0
    while m < t0 + 45:
        b.log(m, svc, "ERROR", "container restarted: OOMKilled (exit code 137)")
        m += 15
    alert = f"ALERT [warning] {svc}: pod restarts 3 in 30m (threshold 2) since {ts(t0 + 30)}"
    ans = _answer("", "memory_leak", svc, ["OOMKilled", "heap usage"], t0, "restart_service", svc)
    return alert, ans


def build_expired_certificate(b: Builder, svc: str, variant: int):
    t0, rng = b.t0, b.rng
    b.shape(svc, "error_rate", t0, WINDOW_MIN, lambda m: rng.uniform(95, 99))
    b.shape(svc, "request_rate", t0, WINDOW_MIN, lambda m: rng.gauss(60, 5))
    b.deploy(t0 - 150, svc, _version(rng), "Bump logging library patch version")  # misleading
    b.config(t0 - 140, svc, "connection_pool_size", "20", "40")  # misleading
    m = t0
    while m < t0 + 60:
        b.log(
            m,
            svc,
            "ERROR",
            "tls handshake failed: x509: certificate has expired or is not yet valid: "
            f"current time {ts(m)} is after {ts(t0)}",
        )
        m += rng.uniform(0.5, 2)
    alert = f"ALERT [critical] {svc}: connection failures 97% (threshold 5%) since {ts(t0)}"
    ans = _answer(
        "", "expired_certificate", svc, ["certificate has expired", "x509"], t0,
        "open_ticket", "renew certificate",
    )
    return alert, ans


def build_slow_query(b: Builder, svc: str, variant: int):
    t0, rng = b.t0, b.rng
    db = svc.split("-")[0] + "-db"
    b.services.append(db)
    b.m[db] = {
        "error_rate": [0.1] * N_POINTS,
        "latency_p95_ms": [rng.gauss(15, 2) for _ in range(N_POINTS)],
        "memory_mb": [rng.gauss(4000, 30) for _ in range(N_POINTS)],
        "cpu_pct": [rng.gauss(30, 4) for _ in range(N_POINTS)],
        "request_rate": [rng.gauss(900, 30) for _ in range(N_POINTS)],
    }
    table, col = ["orders", "customers", "invoices", "reports"][variant], "customer_email"
    b.shape(svc, "latency_p95_ms", t0, WINDOW_MIN, lambda m: rng.gauss(3100, 150))
    b.shape(db, "cpu_pct", t0, WINDOW_MIN, lambda m: rng.gauss(90, 3))
    b.shape(db, "latency_p95_ms", t0, WINDOW_MIN, lambda m: rng.gauss(2800, 150))
    b.shape(svc, "request_rate", t0, WINDOW_MIN, lambda m: rng.gauss(240, 10))  # misleading
    other = rng.choice(NOISE_SERVICES)
    b.deploy(t0 - 30, other, _version(rng), "Tidy imports")  # misleading
    m = t0
    while m < t0 + 60:
        b.log(
            m, db, "WARN",
            f"slow query ({rng.randint(2500, 4800)}ms): SELECT * FROM {table} WHERE {col} "
            f"LIKE '%@example.com'; plan: Seq Scan on {table}",
        )
        m += rng.uniform(1, 3)
    b.log(t0 + 2, svc, "WARN", "request timed out waiting for database (3000ms)")
    alert = f"ALERT [warning] {svc}: p95 latency 3100ms (threshold 800ms) since {ts(t0 + 3)}"
    ans = _answer("", "slow_query", db, ["slow query", "Seq Scan"], t0, "open_ticket", db)
    return alert, ans


def build_feature_flag(b: Builder, svc: str, variant: int):
    t0, rng = b.t0, b.rng
    flag = FLAGS[variant]
    b.config(t0 - 1, svc, flag, "off", "on (100%)")
    b.config(t0 - 90, svc, "request_timeout_ms", "2000", "2000")  # misleading, no-op
    other = rng.choice(NOISE_SERVICES)
    b.deploy(t0 - 15, other, _version(rng), "Fix typo in error message")  # misleading
    err = rng.uniform(12, 20)
    b.shape(svc, "error_rate", t0, WINDOW_MIN, lambda m: err + rng.gauss(0, 1))
    b.shape(svc, "latency_p95_ms", t0, WINDOW_MIN, lambda m: rng.gauss(2100, 100))
    m = t0 + 1
    while m < t0 + 60:
        b.log(m, svc, "ERROR", "timeout in experimental code path (call to model-v2 after 2000ms)")
        m += rng.uniform(1, 3)
    alert = f"ALERT [critical] {svc}: error rate {err:.0f}% (threshold 5%) since {ts(t0 + 2)}"
    ans = _answer("", "feature_flag", svc, [flag], t0, "toggle_feature_flag", flag)
    return alert, ans


def build_dependency_outage(b: Builder, dep: str, variant: int):
    t0, rng = b.t0, b.rng
    front = "api-gateway"
    err = rng.uniform(25, 40)
    b.shape(front, "error_rate", t0, WINDOW_MIN, lambda m: err + rng.gauss(0, 2))
    b.shape(dep, "error_rate", t0, WINDOW_MIN, lambda m: rng.uniform(98, 100))
    b.shape(dep, "request_rate", t0, WINDOW_MIN, lambda m: rng.uniform(0, 3))
    b.deploy(t0 - 25, front, _version(rng), "Bump http client timeout to 5s")  # misleading
    m = t0
    while m < t0 + 60:
        b.log(m, front, "ERROR", f"upstream {dep}: connection refused (dial tcp 10.0.3.14:8443)")
        b.log(m + 0.2, dep, "ERROR", "health check failing: database primary unreachable")
        m += rng.uniform(1, 3)
    alert = f"ALERT [critical] {front}: 5xx rate {err:.0f}% (threshold 5%) since {ts(t0 + 2)}"
    ans = _answer(
        "", "dependency_outage", dep, ["connection refused", "health check failing"], t0,
        "open_ticket", dep,
    )
    return alert, ans


BUILDERS = {
    "bad_deploy": build_bad_deploy,
    "memory_leak": build_memory_leak,
    "expired_certificate": build_expired_certificate,
    "slow_query": build_slow_query,
    "feature_flag": build_feature_flag,
    "dependency_outage": build_dependency_outage,
}


def generate_one(kind: str, variant: int, out_dir: Path) -> dict:
    sid = f"{kind}-{variant}"
    rng = random.Random(f"{kind}-{variant}")
    culprit = POOLS[kind][variant]
    involved = [culprit] + rng.sample([s for s in NOISE_SERVICES if s != culprit], 3)
    if kind == "dependency_outage" and "api-gateway" not in involved:
        involved.append("api-gateway")
    b = Builder(rng, list(dict.fromkeys(involved)))
    alert, answer = BUILDERS[kind](b, culprit, variant)
    answer["id"] = sid
    b.write(out_dir / sid, alert, answer)
    return {"id": sid, "kind": kind, "split": "dev" if variant in DEV_VARIANTS else "heldout"}


def generate_all(out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = [
        generate_one(kind, v, out_dir) for kind in CATEGORIES for v in range(VARIANTS_PER_KIND)
    ]
    _dump(out_dir / "manifest.json", {"scenarios": manifest})
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=Path(__file__).parent / "data")
    args = p.parse_args()
    manifest = generate_all(args.out)
    print(f"Wrote {len(manifest)} scenarios to {args.out}")


if __name__ == "__main__":
    main()
