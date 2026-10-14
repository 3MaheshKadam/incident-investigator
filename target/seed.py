"""Prepare the shared data folder: the orders database and an empty flag file. Safe to repeat.

    python -m target.seed /data --rows 200000
"""

from __future__ import annotations

import argparse
from pathlib import Path

from target.db import ensure_db


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("--rows", type=int, default=200_000)
    args = p.parse_args(argv)
    args.folder.mkdir(parents=True, exist_ok=True)
    ensure_db(args.folder / "shop.sqlite", args.rows)
    flags = args.folder / "flags.json"
    if not flags.exists():
        flags.write_text("{}")
    print(f"seeded {args.folder} with {args.rows} orders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
