"""How the detector behaves as traffic changes, on the real stack.

    python -m perf.detect_bench --loads 1,4,16 --kinds dependency_outage,bad_deploy,slow_query --healthy 1

The detector learns "normal" from the first samples, so a quiet system and a busy one have
different baselines. For each traffic level this runs every fault once plus healthy episodes and
reports whether the fault was detected, how long after users were affected, and any false alarm.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from perf.bench import RESULTS
from target.episode import LIVE_KINDS, run_live_episode
from target.run import score


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--loads", default="1,4,16", help="load generator threads, comma list")
    p.add_argument("--kinds", default=",".join(LIVE_KINDS))
    p.add_argument("--healthy", type=int, default=1)
    p.add_argument("--samples", type=int, default=100)
    p.add_argument("--rows", type=int, default=300_000)
    p.add_argument("--workdir", type=Path, default=Path(".runtime"))
    p.add_argument("--label", default="detect-vs-load")
    args = p.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for threads in (int(x) for x in args.loads.split(",")):
        for kind in [*args.kinds.split(","), *[None] * args.healthy]:
            ep = run_live_episode(kind, 0, args.workdir, args.rows, args.samples, load_threads=threads)
            r = score(ep) | {"load_threads": threads}
            rows.append(r)
            what = r["kind"] or "healthy"
            result = (f"detected ttd={r['ttd_samples']}" if r.get("detected") else "MISSED") if r["kind"] \
                else f"false alarms={r['false_alarms']}"
            print(f"load={threads:<3} {what:<18} {result}", flush=True)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{args.label}.json").write_text(json.dumps(rows, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
