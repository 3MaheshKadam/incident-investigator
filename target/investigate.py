"""Break the real stack, then let the investigator look at it live through the command gateway.

    LLM_PROVIDER=ollama LLM_MODEL=llama3.1:8b python -m target.investigate --kinds dependency_outage

For each fault: run the live episode, record what the monitor saw (logs, metrics, deploys), then
leave the stack running with the fault in place and run the investigator with its usual read-only
tools plus `run_command`. Scored against the injected fault, like every other run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from agent.data import ScenarioData
from agent.env import load_dotenv
from agent.investigator import run_investigation
from agent.llm import make_llm
from evals.score import score_run, summarize
from gateway import CommandGateway
from gateway.tool import GatewayToolbox
from guard.guard import Guard
from monitor.record import record_episode
from target.episode import LIVE_CONFIG, LIVE_KINDS, SAMPLES, run_live_episode


def investigate_live(kind: str, variant: int, llm, workdir: Path, rows: int, samples: int,
                     max_steps: int, use_guard: bool) -> dict:
    out: dict = {}

    def look(stack, ep) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = record_episode(ep, Path(tmp), LIVE_CONFIG)
            if entry is None:
                out["row"] = {"id": ep.id, "kind": kind, "stop_reason": "not_detected"}
                return
            data = ScenarioData(Path(tmp) / entry["id"])
            answer = json.loads((Path(tmp) / entry["id"] / "answer.json").read_text())
            gw = CommandGateway(stack)
            result = run_investigation(llm, data, max_steps, Guard() if use_guard else None,
                                       box=GatewayToolbox(data, gw))
            row = score_run(result, answer)
            commands = [t for t in result.trace if t.get("name") == "run_command"]
            row["commands_run"] = len(commands)
            row["commands_denied"] = sum(a["event"] == "denied" for a in gw.audit)
            row["changes_proposed"] = sum(a["event"] == "proposed" for a in gw.audit)
            row["audit"] = gw.audit
            out["row"], out["result"] = row, result

    run_live_episode(kind, variant, workdir, rows, samples, inspect=look)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--kinds", default="all", help=f"comma list or 'all' ({','.join(LIVE_KINDS)})")
    p.add_argument("--variants", default="0")
    p.add_argument("--rows", type=int, default=300_000)
    p.add_argument("--samples", type=int, default=SAMPLES)
    p.add_argument("--workdir", type=Path, default=Path(".runtime"))
    p.add_argument("--out", type=Path, default=Path("evals/reports/live"))
    p.add_argument("--no-guard", action="store_true")
    args = p.parse_args(argv)
    load_dotenv()
    if os.environ.get("LLM_PROVIDER", "mock").lower() == "mock":
        print("Set LLM_PROVIDER to ollama or openai_compat (see .env.example).", file=sys.stderr)
        return 2
    llm = make_llm()
    kinds = LIVE_KINDS if args.kinds == "all" else args.kinds.split(",")
    max_steps = int(os.environ.get("MAX_STEPS", "12"))
    args.workdir.mkdir(parents=True, exist_ok=True)
    (args.out / "traces").mkdir(parents=True, exist_ok=True)
    rows = []
    for kind in kinds:
        for v in (int(x) for x in args.variants.split(",")):
            print(f"live {kind}-{v} ...", flush=True)
            r = investigate_live(kind, v, llm, args.workdir, args.rows, args.samples,
                                 max_steps, not args.no_guard)
            row = r["row"]
            rows.append(row)
            if "result" in r:
                r["result"].save(args.out / "traces" / f"{row['id']}.json")
            print(f"  {row['id']}: root_cause={row.get('root_cause_correct')} "
                  f"commands={row.get('commands_run', 0)} denied={row.get('commands_denied', 0)} "
                  f"proposed_changes={row.get('changes_proposed', 0)} stop={row['stop_reason']}")
    scored = [r for r in rows if "root_cause_correct" in r]
    if scored:
        print(json.dumps(summarize(scored), indent=1))
    (args.out / "summary.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
