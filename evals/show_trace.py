"""Print one saved run in a readable form.

    python -m evals.show_trace evals/reports/latest/traces/bad_deploy-0.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ui.replay import replay_steps


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    run = json.loads(Path(args[0]).read_text())
    print(f"scenario {run['scenario_id']}  stop={run['stop_reason']}  tool calls={run['steps']}")
    for i, step in enumerate(replay_steps(run["trace"]), 1):
        body = step["body"].replace("\n", "\n      ")
        print(f"\n[{i}] {step['title']}\n      {body[:600]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
