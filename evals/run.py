"""Run the investigator over a split of the scenarios and score it.

Examples:
    python -m evals.run --split dev
    python -m evals.run --split heldout --out evals/reports/final

Use the dev split while tuning. Use heldout only for the final numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent.data import ScenarioData
from agent.investigator import run_investigation
from agent.llm import make_llm
from evals.score import score_run, summarize

DATA_DIR = Path(__file__).resolve().parent.parent / "scenarios" / "data"


def load_manifest(data_dir: Path, split: str) -> list[dict]:
    entries = json.loads((data_dir / "manifest.json").read_text())["scenarios"]
    return [e for e in entries if split == "all" or e["split"] == split]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    p.add_argument("--data", type=Path, default=DATA_DIR)
    p.add_argument("--out", type=Path, default=Path("evals/reports/latest"))
    p.add_argument("--limit", type=int, default=0, help="Run only the first N scenarios")
    args = p.parse_args(argv)

    if os.environ.get("LLM_PROVIDER", "mock").lower() == "mock":
        print("Set LLM_PROVIDER to ollama or openai_compat (see .env.example).", file=sys.stderr)
        return 2

    llm = make_llm()
    max_steps = int(os.environ.get("MAX_STEPS", "12"))
    entries = load_manifest(args.data, args.split)
    if args.limit:
        entries = entries[: args.limit]

    rows = []
    for e in entries:
        folder = args.data / e["id"]
        result = run_investigation(llm, ScenarioData(folder), max_steps=max_steps)
        answer = json.loads((folder / "answer.json").read_text())
        row = score_run(result, answer)
        row["split"] = e["split"]
        rows.append(row)
        result.save(args.out / "traces" / f"{e['id']}.json")
        mark = "ok " if row["root_cause_correct"] else "BAD"
        print(f"{mark} {e['id']:<28} steps={row['steps']:<3} stop={row['stop_reason']}")

    summary = summarize(rows)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(
        json.dumps({"split": args.split, "summary": summary, "runs": rows}, indent=1) + "\n"
    )
    n = summary["n"]
    print(f"\n{args.split}: root cause {summary['root_cause_correct']}/{n}, "
          f"evidence {summary['evidence_cited']}/{n}, wrong blame {summary['wrong_blame']}/{n}, "
          f"no report {summary['no_report']}/{n}, avg steps {summary['avg_steps']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
