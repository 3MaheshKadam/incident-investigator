"""Controlled failures. Each one changes a service's state; the symptoms come from the simulation.

`truth()` is what the evaluation scores against (the same shape as a scenario's answer.json). It
is kept out of everything the investigator can read.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from shop.system import ShopSystem, ts

KINDS = [
    "bad_deploy",
    "memory_leak",
    "expired_certificate",
    "slow_query",
    "feature_flag",
    "dependency_outage",
]
FLAGS = ["fast_checkout_path", "new_ranking_model", "async_feed_render", "recs_v2_pipeline"]
TABLES = ["orders", "customers", "invoices", "reports"]
DOWN_REASONS = {
    "orders-db": "database primary unreachable",
    "catalog-db": "database primary unreachable",
    "payments-gateway": "upstream provider unreachable",
    "cache-proxy": "redis cluster unreachable",
}


def version(rng: random.Random) -> str:
    return f"v{rng.randint(1, 4)}.{rng.randint(1, 20)}.{rng.randint(0, 9)}"


@dataclass
class Fault:
    kind: str
    service: str  # where the fault is injected (the real root cause)
    at: int  # minute it is injected
    params: dict = field(default_factory=dict)

    def inject(self, system: ShopSystem) -> None:
        p, svc = self.params, system.services[self.service]
        k = self.kind
        if k == "bad_deploy":
            system.at(self.at, lambda sy, m: sy.deploy(m, self.service, p["version"], p["commit"]))
            system.at(self.at + 1, lambda sy, m: setattr(svc, "bug_rate", p["rate"]))
        elif k == "memory_leak":
            system.at(self.at, lambda sy, m: setattr(svc, "leak_mb_per_min", p["rate"]))
        elif k == "expired_certificate":
            system.at(self.at, lambda sy, m: setattr(svc, "cert_expired_at", m))
        elif k == "slow_query":
            system.at(self.at, lambda sy, m: setattr(svc, "slow_table", p["table"]))
        elif k == "feature_flag":
            system.at(
                self.at, lambda sy, m: sy.config(m, self.service, p["flag"], "off", "on (100%)")
            )
            system.at(self.at + 1, lambda sy, m: setattr(svc, "flag_err_rate", p["rate"]))
        elif k == "dependency_outage":
            system.at(self.at, lambda sy, m: setattr(svc, "down_reason", p["reason"]))
        else:
            raise ValueError(f"unknown fault kind {k!r}")

    def impact_start(self, system: ShopSystem) -> int | None:
        """The minute users first feel it. For a leak that is the first out-of-memory kill."""
        if self.kind == "memory_leak":
            return system.first_oom.get(self.service)
        return self.at + (1 if self.kind in ("bad_deploy", "feature_flag") else 0)

    def truth(self, system: ShopSystem, sid: str) -> dict:
        p, k = self.params, self.kind
        evidence = {
            "bad_deploy": [p.get("version"), "NullPointerException"],
            "memory_leak": ["OOMKilled", "heap usage"],
            "expired_certificate": ["certificate has expired", "x509"],
            "slow_query": ["slow query", "Seq Scan"],
            "feature_flag": [p.get("flag")],
            "dependency_outage": ["connection refused", "health check failing"],
        }[k]
        action = {
            "bad_deploy": ("rollback_deploy", self.service),
            "memory_leak": ("restart_service", self.service),
            "expired_certificate": ("open_ticket", "renew certificate"),
            "slow_query": ("open_ticket", self.service),
            "feature_flag": ("toggle_feature_flag", p.get("flag")),
            "dependency_outage": ("open_ticket", self.service),
        }[k]
        start = self.impact_start(system)
        return {
            "id": sid,
            "kind": k,
            "root_cause_category": k,
            "component": self.service,
            "evidence_keys": evidence,
            "incident_start": ts(start) if start is not None else None,
            "injected_at": ts(self.at),
            "correct_action": {"type": action[0], "target": action[1]},
        }


def make_fault(
    kind: str, service: str, at: int, rng: random.Random, variant: int, severity: float = 1.0
) -> Fault:
    params: dict = {}
    if kind == "bad_deploy":
        params = {
            "version": version(rng),
            "commit": "Refactor request handling and add currency conversion",
            "rate": rng.uniform(0.10, 0.25) * severity,
        }
    elif kind == "memory_leak":
        params = {"rate": rng.uniform(14, 18) * severity}
    elif kind == "slow_query":
        params = {"table": TABLES[variant % len(TABLES)]}
    elif kind == "feature_flag":
        params = {"flag": FLAGS[variant % len(FLAGS)], "rate": rng.uniform(0.12, 0.20) * severity}
    elif kind == "dependency_outage":
        params = {"reason": DOWN_REASONS[service]}
    return Fault(kind, service, at, params)
