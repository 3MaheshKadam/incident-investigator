"""Does a harmless traffic surge look like a fault to the detector?

    python -m perf.surge_check                    # current code
    DB_POOL_SIZE=1000 python -m perf.surge_check  # the old unbounded pool, for comparison

Runs a healthy system whose traffic jumps from 4 to 4+N users (default N=24) halfway through (after the detector
has learned its baseline). Any incident raised is a false alarm.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from target.episode import run_live_episode
from target.run import score


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--samples", type=int, default=100)
    p.add_argument("--surge-at", type=int, default=50)
    p.add_argument("--surge-threads", type=int, default=24, help="extra users arriving at the surge")
    p.add_argument("--rows", type=int, default=300_000)
    p.add_argument("--workdir", type=Path, default=Path(".runtime"))
    args = p.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)
    ep = run_live_episode(None, 0, args.workdir, args.rows, args.samples, surge_at=args.surge_at,
                          surge_threads=args.surge_threads)
    r = score(ep)
    print(f"DB_POOL_SIZE={os.environ.get('DB_POOL_SIZE', 'default')} false alarms: {r['false_alarms']}")
    for a in r["alerts"]:
        print("  ", a)
    return 1 if r["false_alarms"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
