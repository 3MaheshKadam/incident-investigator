"""Measure how often the agent obeys planted injections, with the guard off and on.

    python -m evals.attack_run --split dev --attacks-per-scenario 3

Four conditions over the same scenarios (and the same attack assignment):
  clean_off   no attacks, guard off   reference accuracy
  attack_off  attacks, guard off      the "before" number
  attack_on   attacks, guard on       the "after" number
  clean_on    no attacks, guard on    what the guard wrongly blocks or changes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.data import ScenarioData
from agent.env import load_dotenv
from agent.investigator import GuardLike, run_investigation
from agent.llm import LLM, make_llm
from evals.run import DATA_DIR, load_manifest
from evals.score import score_run
from guard.attacks import Attack, all_attacks, attack_succeeded, plant
from guard.guard import Guard


def assign_attacks(
    entries: list[dict], attacks: list[Attack], per_scenario: int
) -> list[tuple[dict, Attack | None]]:
    """Deterministic scenario/attack pairs. per_scenario=0 means every attack on every scenario."""
    if per_scenario <= 0:
        return [(e, a) for e in entries for a in attacks]
    return [
        (e, attacks[(i * per_scenario + k) % len(attacks)])
        for i, e in enumerate(entries)
        for k in range(per_scenario)
    ]


def run_condition(
    llm: LLM,
    jobs: list[tuple[dict, Attack | None]],
    data_dir: Path,
    max_steps: int,
    guard_factory: Callable[[], GuardLike] | None,
) -> list[dict[str, Any]]:
    rows = []
    for entry, attack in jobs:
        folder = data_dir / entry["id"]
        data = ScenarioData(folder)
        answer = json.loads((folder / "answer.json").read_text())
        planted = plant(attack, data, answer) if attack else None
        guard = guard_factory() if guard_factory else None
        result = run_investigation(
            llm, planted.data if planted else data, max_steps=max_steps, guard=guard
        )
        row = score_run(result, answer)
        row["attack"] = attack.id if attack else None
        row["obeyed"] = bool(planted and attack_succeeded(planted, result.report))
        row["guard_blocks"] = result.guard_blocks
        row["guard_redactions"] = result.guard_redactions
        row["legit_blocked"] = any(
            t["type"] == "guard_block"
            and t["arguments"]["action_type"] == answer["correct_action"]["type"]
            and t["arguments"]["component"].lower() == answer["component"].lower()
            for t in result.trace
        )
        rows.append(row)
    return rows


def summarize_condition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "obeyed": sum(r["obeyed"] for r in rows),
        "root_cause_correct": sum(r["root_cause_correct"] for r in rows),
        "guard_blocks": sum(r["guard_blocks"] for r in rows),
        "guard_redactions": sum(r["guard_redactions"] for r in rows),
        "legit_blocked": sum(r["legit_blocked"] for r in rows),
        "no_report": sum(r["stop_reason"] != "report" for r in rows),
    }


def run_all(
    llm: LLM, entries: list[dict], data_dir: Path, per_scenario: int, max_steps: int
) -> dict[str, dict[str, Any]]:
    attacked = assign_attacks(entries, all_attacks(), per_scenario)
    clean = [(e, None) for e in entries]
    plan = {
        "clean_off": (clean, None),
        "attack_off": (attacked, None),
        "attack_on": (attacked, Guard),
        "clean_on": (clean, Guard),
    }
    out = {}
    for name, (jobs, factory) in plan.items():
        rows = run_condition(llm, jobs, data_dir, max_steps, factory)
        out[name] = {"summary": summarize_condition(rows), "runs": rows}
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    p.add_argument("--data", type=Path, default=DATA_DIR)
    p.add_argument("--out", type=Path, default=Path("evals/reports/attack"))
    p.add_argument("--attacks-per-scenario", type=int, default=3, help="0 = every attack on every scenario")
    args = p.parse_args(argv)
    load_dotenv()

    if os.environ.get("LLM_PROVIDER", "mock").lower() == "mock":
        print("Set LLM_PROVIDER to ollama or openai_compat (see .env.example).", file=sys.stderr)
        return 2

    llm = make_llm()
    max_steps = int(os.environ.get("MAX_STEPS", "12"))
    entries = load_manifest(args.data, args.split)
    result = run_all(llm, entries, args.data, args.attacks_per_scenario, max_steps)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(result, indent=1) + "\n")
    s = {k: v["summary"] for k, v in result.items()}
    print(f"split={args.split}  attack runs={s['attack_off']['n']}  clean runs={s['clean_off']['n']}")
    print(f"obeyed injections   guard off: {s['attack_off']['obeyed']}/{s['attack_off']['n']}"
          f"   guard on: {s['attack_on']['obeyed']}/{s['attack_on']['n']}")
    print(f"root cause correct  clean: {s['clean_off']['root_cause_correct']}/{s['clean_off']['n']}"
          f"   attacked, guard off: {s['attack_off']['root_cause_correct']}/{s['attack_off']['n']}"
          f"   attacked, guard on: {s['attack_on']['root_cause_correct']}/{s['attack_on']['n']}")
    c = s["clean_on"]
    print(f"false blocks (clean runs, guard on): legit report blocked {c['legit_blocked']}/{c['n']},"
          f" lines removed {c['guard_redactions']}, accuracy {c['root_cause_correct']}/{c['n']}"
          f" vs {s['clean_off']['root_cause_correct']}/{s['clean_off']['n']} with guard off")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
