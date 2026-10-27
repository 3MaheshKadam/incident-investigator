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
from agent.env import load_dotenv
from agent.investigator import RunResult, run_investigation
from agent.llm import LLMRequestError, make_llm
from evals.score import score_run, summarize
from retrieval.tools import RetrievalToolbox

DATA_DIR = Path(__file__).resolve().parent.parent / "scenarios" / "data"


def load_manifest(data_dir: Path, split: str) -> list[dict]:
    entries = json.loads((data_dir / "manifest.json").read_text())["scenarios"]
    return [e for e in entries if split == "all" or e["split"] == split]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    p.add_argument("--data", type=Path, default=DATA_DIR)
    p.add_argument("--out", type=Path, default=Path("evals/reports/latest"))
    p.add_argument(
        "--resume", action="store_true", help="Reuse finished traces already saved in --out"
    )
    p.add_argument("--limit", type=int, default=0, help="Run only the first N scenarios")
    p.add_argument("--retrieval", action="store_true",
                   help="Also give the model find_evidence and suggest_hypotheses (retrieval tools)")
    args = p.parse_args(argv)
    load_dotenv()

    if os.environ.get("LLM_PROVIDER", "mock").lower() == "mock":
        print("Set LLM_PROVIDER to ollama or openai_compat (see .env.example).", file=sys.stderr)
        return 2

    llm = make_llm()
    max_steps = int(os.environ.get("MAX_STEPS", "12"))
    entries = load_manifest(args.data, args.split)
    if args.limit:
        entries = entries[: args.limit]

    rows = []
    stopped_early = False
    for e in entries:
        folder = args.data / e["id"]
        trace_file = args.out / "traces" / f"{e['id']}.json"
        result = None
        if args.resume and trace_file.is_file():
            saved = json.loads(trace_file.read_text())
            if saved.get("stop_reason") != "error":
                result = RunResult(**saved)
                print(f"reusing {e['id']}", flush=True)
        if result is None:
            print(f"running {e['id']} ...", flush=True)
            try:
                data = ScenarioData(folder)
                box = RetrievalToolbox(data, args.data) if args.retrieval else None
                result = run_investigation(llm, data, max_steps=max_steps, box=box)
            except LLMRequestError as exc:
                if exc.status == 429 and "per day" in exc.body.lower():
                    print("\nDaily token limit reached. Stopping; rerun later with --resume.")
                    stopped_early = True
                    break
                print(f"      error: {exc}"[:300], flush=True)
                result = RunResult(e["id"], None, 0, "error")
            except Exception as exc:  # noqa: BLE001  one failed call must not lose the run
                print(f"      error: {exc}"[:300], flush=True)
                result = RunResult(e["id"], None, 0, "error")
        answer = json.loads((folder / "answer.json").read_text())
        row = score_run(result, answer)
        row["split"] = e["split"]
        rows.append(row)
        result.save(args.out / "traces" / f"{e['id']}.json")
        mark = "ok " if row["root_cause_correct"] else "BAD"
        print(f"{mark} {e['id']:<28} steps={row['steps']:<3} stop={row['stop_reason']}")
        if result.report is None:
            said = [t["text"] for t in result.trace if t["type"] == "model_text"]
            if said:
                print(f"      model said: {said[-1][:200]!r}")

    if stopped_early and not rows:
        return 1
    summary = summarize(rows)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(
        json.dumps({"split": args.split, "summary": summary, "runs": rows}, indent=1) + "\n"
    )
    n = summary["n"]
    if stopped_early:
        print(f"\nPARTIAL: only {summary['n']} of {len(entries)} scenarios finished.")
    print(f"\n{args.split}: root cause {summary['root_cause_correct']}/{n}, "
          f"evidence {summary['evidence_cited']}/{n}, wrong blame {summary['wrong_blame']}/{n}, "
          f"no report {summary['no_report']}/{n}, avg steps {summary['avg_steps']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
