"""Run live episodes against the real stack, score detection, and record the incidents.

    python -m target.run --kinds all --variants 0,1 --healthy 1 --out scenarios/live

Needs Linux (it reads /proc). An episode takes about 75 seconds of real time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.detect_eval import SLACK_MIN
from monitor.detect import detect_incidents
from monitor.record import record_episode
from target.episode import LIVE_CONFIG, LIVE_KINDS, SAMPLES, run_live_episode


def score(ep) -> dict:
    s, f = ep.system, ep.fault
    incidents = detect_incidents(s.metrics, s.deploys, s.config_changes, LIVE_CONFIG)
    if f is None:
        return {"id": ep.id, "kind": None, "false_alarms": len(incidents),
                "alerts": [i.alert for i in incidents]}
    impact = f.impact_start(s)
    hi = impact + SLACK_MIN if impact is not None else 10**6
    hits = [i for i in incidents if f.at <= i.detected_minute <= hi]
    first = min(hits, key=lambda i: i.detected_minute) if hits else None
    return {
        "id": ep.id, "kind": f.kind, "detected": first is not None,
        "ttd_samples": (first.detected_minute - impact) if first and impact is not None else None,
        "alert": first.alert if first else None,
        "extra_incidents": len(incidents) - len(hits),
        "suspect_changes": len(first.suspects) if first else 0,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--kinds", default="all", help=f"comma list or 'all' ({','.join(LIVE_KINDS)})")
    p.add_argument("--variants", default="0", help="comma list, for example 0,1")
    p.add_argument("--healthy", type=int, default=0, help="number of fault-free episodes")
    p.add_argument("--samples", type=int, default=SAMPLES)
    p.add_argument("--rows", type=int, default=1_000_000, help="rows in the orders table")
    p.add_argument("--workdir", type=Path, default=Path(".runtime"))
    p.add_argument("--out", type=Path, default=None, help="record detected incidents here")
    p.add_argument("--require-detected", action="store_true",
                   help="Exit 1 if any fault is missed or any healthy episode raises an alarm (for CI)")
    p.add_argument("--summary", type=Path, default=None, help="write the detection summary as JSON")
    args = p.parse_args(argv)

    kinds = LIVE_KINDS if args.kinds == "all" else args.kinds.split(",")
    variants = [int(v) for v in args.variants.split(",")]
    args.workdir.mkdir(parents=True, exist_ok=True)
    work = args.workdir
    rows, manifest = [], []
    for kind in kinds:
        for v in variants:
            print(f"running {kind}-{v} ...", flush=True)
            ep = run_live_episode(kind, v, work, args.rows, args.samples)
            r = score(ep)
            rows.append(r)
            mark = "ok  " if r["detected"] else "MISS"
            print(f"{mark} {ep.id:<26} ttd={r['ttd_samples']!s:<4} extra={r['extra_incidents']} {r['alert'] or ''}")
            if args.out:
                entry = record_episode(ep, args.out, LIVE_CONFIG)
                if entry:
                    manifest.append(entry)
    for v in range(args.healthy):
        print(f"running healthy-{v} ...", flush=True)
        ep = run_live_episode(None, v, work, args.rows, args.samples)
        r = score(ep)
        rows.append(r)
        print(f"{'ok  ' if not r['false_alarms'] else 'FALSE'} {ep.id:<26} false alarms={r['false_alarms']} {r['alerts']}")

    faults = [r for r in rows if r["kind"]]
    healthy = [r for r in rows if r["kind"] is None]
    detected = [r for r in faults if r["detected"]]
    print(f"\nlive: detected {len(detected)}/{len(faults)}, "
          f"false alarms on healthy {sum(r['false_alarms'] for r in healthy)}/{len(healthy)} episodes, "
          f"extra incidents {sum(r['extra_incidents'] for r in faults)}")
    if args.out and manifest:
        (args.out / "manifest.json").write_text(json.dumps({"scenarios": manifest}, indent=1) + "\n")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(rows, indent=1) + "\n")
    bad = len(faults) - len(detected) + sum(r["false_alarms"] for r in healthy)
    return 1 if args.require_detected and bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
