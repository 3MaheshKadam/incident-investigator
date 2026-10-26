"""Steady user traffic against a gateway URL, for a deployed stack.

    python -m target.loadgen http://api-gateway:8000 --threads 4
"""

from __future__ import annotations

import argparse
import time

from target.load import Load


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url")
    p.add_argument("--threads", type=int, default=4)
    args = p.parse_args(argv)
    load = Load(args.url, args.threads)
    load.start()
    print(f"sending traffic to {args.url} with {args.threads} threads", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        load.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
