"""Anomaly detection and incident grouping.

The detector only sees metrics and change events (deploys, config changes). It never sees the
fault definitions. For each service and metric it learns what "normal" looks like from the first
`TRAIN_MIN` minutes (median and spread), then raises an anomaly when a value is far outside
normal *and* the change is big enough to matter, for several minutes in a row.

Why both conditions: a statistical test alone fires on tiny wobbles in very quiet metrics, and a
fixed threshold alone cannot tell a quiet service from a noisy one. Requiring both keeps false
alarms down. Harmless events, like a traffic surge, change neither error rate, latency nor memory
enough to count.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import median

from shop.system import START, ts

TRAIN_MIN = 30  # minutes of data used to learn "normal"
CONSECUTIVE = 3  # minutes in a row a value must be abnormal
K = 6.0  # how many robust standard deviations away counts as abnormal
REARM_MIN = 10  # an anomaly closes after this many normal minutes
GROUP_GAP_MIN = 30  # anomalies this close together belong to one incident
REFIRE_WINDOW_MIN = 120  # a service already in an open incident firing again is the same incident
SUSPECT_BEFORE_MIN = 30  # changes this long before an incident are suspects
USER_FACING = "api-gateway"


@dataclass(frozen=True)
class Rule:
    min_delta: float  # smallest absolute rise that matters
    scale_floor: float  # lower bound on the "normal spread", so quiet metrics do not explode
    min_ratio: float = 1.0  # value must also be at least this multiple of normal
    threshold: float = 1.0  # used only to rank how bad an anomaly is


RULES = {
    "error_rate": Rule(min_delta=3.0, scale_floor=0.2, threshold=5.0),
    "latency_p95_ms": Rule(min_delta=150.0, scale_floor=5.0, min_ratio=2.0, threshold=800.0),
    "memory_mb": Rule(min_delta=400.0, scale_floor=10.0, threshold=2048.0),
}


@dataclass
class Anomaly:
    service: str
    metric: str
    start: int
    detected: int
    value: float
    baseline: float
    severity: float


@dataclass
class Incident:
    id: str
    detected_minute: int
    start_minute: int
    alert: str
    symptom_service: str
    symptom_metric: str
    anomalies: list[Anomaly] = field(default_factory=list)
    suspects: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def minute_of(stamp: str) -> int:
    t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return int((t - START).total_seconds() // 60)


def _normal(values: list[float], rule: Rule) -> tuple[float, float]:
    med = median(values)
    spread = 1.4826 * median(abs(v - med) for v in values)
    return med, max(spread, rule.scale_floor)


def _abnormal(x: float, med: float, spread: float, rule: Rule) -> bool:
    rise = x - med
    return rise >= rule.min_delta and rise / spread >= K and x >= med * rule.min_ratio


def find_anomalies(metrics: dict[str, dict[str, list[float]]]) -> list[Anomaly]:
    found: list[Anomaly] = []
    for service, series in metrics.items():
        for metric, rule in RULES.items():
            values = series.get(metric, [])
            if len(values) <= TRAIN_MIN + CONSECUTIVE:
                continue
            med, spread = _normal(values[:TRAIN_MIN], rule)
            streak, calm, open_since = 0, 0, None
            for m in range(TRAIN_MIN, len(values)):
                if _abnormal(values[m], med, spread, rule):
                    streak, calm = streak + 1, 0
                    if streak == CONSECUTIVE and open_since is None:
                        open_since = m - CONSECUTIVE + 1
                        found.append(
                            Anomaly(
                                service, metric, open_since, m, values[m], round(med, 2),
                                round(values[m] / rule.threshold, 3),
                            )
                        )
                else:
                    streak, calm = 0, calm + 1
                    if calm >= REARM_MIN:
                        open_since = None
    return sorted(found, key=lambda a: (a.detected, a.service))


def _alert_text(a: Anomaly) -> str:
    """Say what is abnormal and what normal looked like, so the number can be judged."""
    since = ts(a.start)
    if a.metric == "error_rate":
        level = "critical" if a.value >= 10 else "warning"
        return (
            f"ALERT [{level}] {a.service}: error rate {a.value:.0f}% "
            f"(normally {a.baseline:.1f}%) since {since}"
        )
    if a.metric == "latency_p95_ms":
        return (
            f"ALERT [warning] {a.service}: p95 latency {a.value:.0f}ms "
            f"(normally {a.baseline:.0f}ms) since {since}"
        )
    return (
        f"ALERT [warning] {a.service}: memory {a.value:.0f}MB "
        f"(normally {a.baseline:.0f}MB) since {since}"
    )


def detect_incidents(
    metrics: dict[str, dict[str, list[float]]],
    deploys: list[dict],
    config_changes: list[dict],
) -> list[Incident]:
    groups: list[list[Anomaly]] = []
    for a in find_anomalies(metrics):
        if groups:
            g = groups[-1]
            near = a.start <= max(x.start for x in g) + GROUP_GAP_MIN
            refire = a.start <= min(x.start for x in g) + REFIRE_WINDOW_MIN and any(
                x.service == a.service for x in g
            )
            if near or refire:
                g.append(a)
                continue
        groups.append([a])

    incidents = []
    for i, g in enumerate(groups):
        first = min(x.detected for x in g)
        # Page on the user-facing service if it is affected, as an SLO alert would.
        facing = [x for x in g if x.service == USER_FACING]
        symptom = min(facing, key=lambda x: x.detected) if facing else max(g, key=lambda x: x.severity)
        start = min(x.start for x in g)
        changes = [
            {**c, "kind": "deploy" if "version" in c else "config"}
            for c in (*deploys, *config_changes)
            if start - SUSPECT_BEFORE_MIN <= minute_of(c["ts"]) <= start + 1
        ]
        incidents.append(
            Incident(
                id=f"inc-{i}",
                detected_minute=first,
                start_minute=start,
                alert=_alert_text(symptom),
                symptom_service=symptom.service,
                symptom_metric=symptom.metric,
                anomalies=g,
                suspects=sorted(changes, key=lambda c: c["ts"]),
            )
        )
    return incidents
