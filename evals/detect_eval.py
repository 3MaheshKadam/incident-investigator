"""Measure the monitor: does it find injected faults, how fast, and does it stay quiet when healthy?

    python -m evals.detect_eval --split dev
    python -m evals.detect_eval --split heldout

A fault counts as *detected* if an incident is raised between the minute it was injected and 30
minutes after users first feel it. An incident outside every fault window is a *false alarm*.
Time to detection is measured from the first user impact; negative means an early warning.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, median

from monitor.detect import detect_incidents, minute_of
from shop.episode import build_episode
from shop.faults import KINDS

VARIANTS = {"dev": {"fault": [0, 1], "healthy": [0, 1, 2, 3]},
            "heldout": {"fault": [2, 3], "healthy": [4, 5, 6, 7]}}
SLACK_MIN = 30


def evaluate_split(split: str, severity: float = 1.0) -> dict:
    splits = ["dev", "heldout"] if split == "all" else [split]
    rows, false_alarms, healthy_n = [], 0, 0
    for sp in splits:
        for kind in KINDS:
            for v in VARIANTS[sp]["fault"]:
                rows.append(_fault_row(build_episode(kind, v, severity)))
        for v in VARIANTS[sp]["healthy"]:
            ep = build_episode(None, v)
            s = ep.system
            false_alarms += len(detect_incidents(s.metrics, s.deploys, s.config_changes))
            healthy_n += 1

    detected = [r for r in rows if r["detected"]]
    extra_alarms = sum(r["unmatched_incidents"] for r in rows)
    matched = sum(1 for r in rows if r["detected"])
    alarms = matched + extra_alarms + false_alarms
    changed = [r for r in rows if r["change_caused"]]
    ttds = [r["ttd"] for r in detected if r["ttd"] is not None]
    return {
        "split": split,
        "severity": severity,
        "faults": len(rows),
        "detected": len(detected),
        "recall": round(len(detected) / len(rows), 3),
        "precision": round(matched / alarms, 3) if alarms else None,
        "false_alarms_on_healthy": f"{false_alarms}/{healthy_n} episodes",
        "ttd_median_min": median(ttds) if ttds else None,
        "ttd_mean_min": round(mean(ttds), 1) if ttds else None,
        "early_warnings": sum(1 for t in ttds if t < 0),
        "change_correlation": f"{sum(r['cause_in_suspects'] for r in changed)}/{len(changed)}",
        "per_kind": _per_kind(rows),
        "runs": rows,
    }


def _fault_row(ep) -> dict:
    s, f = ep.system, ep.fault
    incidents = detect_incidents(s.metrics, s.deploys, s.config_changes)
    impact = f.impact_start(s)
    lo = f.at
    hi = impact + SLACK_MIN if impact is not None else 10**6  # no crash: any alarm after the start
    hits = [i for i in incidents if lo <= i.detected_minute <= hi]
    first = min(hits, key=lambda i: i.detected_minute) if hits else None
    change_caused = f.kind in ("bad_deploy", "feature_flag")
    cause_in = False
    if first and change_caused:
        cause_in = any(
            c["service"] == f.service and abs(minute_of(c["ts"]) - f.at) <= 1
            for c in first.suspects
        )
    return {
        "id": ep.id,
        "kind": f.kind,
        "detected": first is not None,
        "ttd": (first.detected_minute - impact) if first and impact is not None else None,
        "alert": first.alert if first else None,
        "unmatched_incidents": len(incidents) - len(hits),
        "change_caused": change_caused,
        "cause_in_suspects": cause_in,
        "suspects": len(first.suspects) if first else 0,
    }


def _per_kind(rows: list[dict]) -> dict:
    return {
        k: f"{sum(r['detected'] for r in rows if r['kind'] == k)}/"
        f"{sum(1 for r in rows if r['kind'] == k)}"
        for k in KINDS
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    p.add_argument("--severity", type=float, default=1.0, help="1.0 = loud, 0.1 = very quiet")
    p.add_argument("--sweep", action="store_true", help="Run several severities and print a table")
    p.add_argument("--out", type=Path, default=None, help="Write the full result as JSON")
    args = p.parse_args(argv)
    if args.sweep:
        print(f"{'severity':>8} {'recall':>8} {'precision':>10} {'false alarms':>13} {'median ttd':>11}")
        sweep = [evaluate_split(args.split, sev) for sev in (1.0, 0.5, 0.25, 0.1)]
        for r in sweep:
            print(f"{r['severity']:>8} {r['detected']:>4}/{r['faults']:<3} {r['precision']!s:>10} "
                  f"{r['false_alarms_on_healthy']:>13} {r['ttd_median_min']!s:>11}")
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(sweep, indent=1) + "\n")
        return 0
    res = evaluate_split(args.split, args.severity)
    for r in res["runs"]:
        mark = "ok " if r["detected"] else "MISS"
        print(f"{mark} {r['id']:<26} ttd={r['ttd']!s:<5} {r['alert'] or ''}")
    print(
        f"\n{args.split}: recall {res['detected']}/{res['faults']}, precision {res['precision']}, "
        f"false alarms on healthy {res['false_alarms_on_healthy']}, "
        f"time to detect median {res['ttd_median_min']} min (mean {res['ttd_mean_min']}), "
        f"early warnings {res['early_warnings']}, change correlated {res['change_correlation']}"
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(res, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
