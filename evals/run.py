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
from agent.investigator import RunResult, build_system, first_message, run_investigation
from agent.llm import LLMRequestError, make_llm
from agent.tools import TOOL_SPECS
from evals.score import score_run, summarize
from retrieval.tools import RETRIEVAL_HINT, RetrievalToolbox

DATA_DIR = Path(__file__).resolve().parent.parent / "scenarios" / "data"


def load_manifest(data_dir: Path, split: str) -> list[dict]:
    entries = json.loads((data_dir / "manifest.json").read_text())["scenarios"]
    return [e for e in entries if split == "all" or e["split"] == split]


def select_entries(entries: list[dict], only: str = "", limit: int = 0) -> list[dict]:
    """Keep entries whose id starts with one of the comma-separated prefixes in `only`, then the first `limit`."""
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    if wanted:
        entries = [e for e in entries if any(e["id"].startswith(w) for w in wanted)]
    return entries[:limit] if limit else entries


def dry_run(entries: list[dict], data_dir: Path, retrieval: bool, hint: bool, max_steps: int) -> None:
    """Print what a run would send, without building a model client or calling any model.

    Tool names come from the specs alone; the retrieval toolbox is not built, since it can call an
    embedding model.
    """
    specs = TOOL_SPECS + (RetrievalToolbox.extra_specs if retrieval else [])
    system = build_system(RETRIEVAL_HINT if hint else "")
    fixed = len(system) + len(json.dumps(specs))  # sent on every model call
    print(f"DRY RUN: no model calls. provider={os.environ.get('LLM_PROVIDER', 'mock')} "
          f"model={os.environ.get('LLM_MODEL', '?')} max_steps={max_steps}\n")
    print(f"scenarios ({len(entries)}): {', '.join(e['id'] for e in entries)}")
    print(f"tools offered ({len(specs)}): {', '.join(t['name'] for t in specs)}")
    print(f"retrieval hint included: {'yes' if hint else 'no'}\n")
    print("system prompt:\n" + "-" * 72 + f"\n{system}\n" + "-" * 72 + "\n")
    first_call = 0
    for e in entries:
        chars = fixed + len(first_message(ScenarioData(data_dir / e["id"])))
        first_call += chars // 4
        print(f"  {e['id']:<28} first call ~{chars // 4:>6} input tokens")
    print(f"\nESTIMATE (characters / 4; tool outputs not included):\n"
          f"  first model call, all scenarios:          ~{first_call} input tokens\n"
          f"  if every scenario used all {max_steps} calls: at least ~{first_call * max_steps} input tokens "
          f"(each call resends the prompt, tools and all earlier tool output)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    p.add_argument("--data", type=Path, default=DATA_DIR)
    p.add_argument("--out", type=Path, default=Path("evals/reports/latest"))
    p.add_argument(
        "--resume", action="store_true", help="Reuse finished traces already saved in --out"
    )
    p.add_argument("--limit", type=int, default=0, help="Run only the first N scenarios")
    p.add_argument("--only", default="",
                   help="Comma-separated scenario ids or id prefixes to run, e.g. slow_query or slow_query-0,memory_leak-1")
    p.add_argument("--retrieval", action="store_true",
                   help="Also give the model find_evidence and suggest_hypotheses (retrieval tools)")
    p.add_argument("--retrieval-hint", action="store_true",
                   help="With --retrieval, also tell the model in its instructions when to use the retrieval tools")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the scenarios, tools, system prompt and a token estimate; make no model calls")
    args = p.parse_args(argv)
    if args.retrieval_hint and not args.retrieval:
        p.error("--retrieval-hint needs --retrieval")
    load_dotenv()

    max_steps = int(os.environ.get("MAX_STEPS", "12"))
    entries = load_manifest(args.data, args.split)
    entries = select_entries(entries, args.only, args.limit)
    if not entries:
        print(f"--only {args.only!r} matches no scenario in the {args.split} split.", file=sys.stderr)
        return 2
    if args.dry_run:
        dry_run(entries, args.data, args.retrieval, args.retrieval_hint, max_steps)
        return 0

    if os.environ.get("LLM_PROVIDER", "mock").lower() == "mock":
        print("Set LLM_PROVIDER to ollama or openai_compat (see .env.example).", file=sys.stderr)
        return 2

    llm = make_llm()

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
                result = run_investigation(llm, data, max_steps=max_steps, box=box,
                                           system_extra=RETRIEVAL_HINT if args.retrieval_hint else "")
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
