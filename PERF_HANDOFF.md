# Perf measurement handoff (read this inside Ubuntu/WSL2)

Do NOT commit this file. Delete it when done.

## Why this file exists
The first session ran on native Windows and stopped at setup. The test suite cannot pass there:
`python -m pytest -q` gave **5 failed, 81 passed, 5 skipped, 54 errors**.
- 54 errors: `OSError [WinError 1314]` creating symlinks in `tests/test_gateway.py` (no symlink privilege).
- 5 failures: `test_deploy.py` (docker stack, aws shell syntax), `test_readiness.py`, `test_target.py` (prometheus round trip, http client). Linux paths / tools missing.

So everything below must run in WSL2 Ubuntu. No code or results have been produced yet.

## State of the repo
- Path (Windows): `C:\Users\Admin\Desktop\AI-2026\incident-investigator`
  In Ubuntu: `/mnt/c/Users/Admin/Desktop/AI-2026/incident-investigator`.
  **Better: clone into the Linux filesystem** (`~/incident-investigator`) for honest I/O numbers. /mnt/c is slow and would distort benchmarks.
- Branch `feat/phase-a-perf` is checked out and up to date with origin. `git fetch` already done.
- `evals/reports/` is untracked and unrelated. Leave it alone.
- A Windows `.venv/` exists in the Windows folder (git-ignored). Do not reuse it in Linux. Use a fresh venv in the Linux clone.
- Windows host has Python 3.14 only. Docker Desktop 29.8.2 / Compose v5.5.1 installed, **daemon was stopped**. Start Docker Desktop and enable WSL integration for the Ubuntu distro (Settings > Resources > WSL integration) before Part 1B.

## Hard rules
- Never read, print, copy or commit `.env` or any API key. Run nothing that needs a model key.
- Commits/PRs: author Mahesh Kadam <maheshkadam9298@gmail.com>. No Co-Authored-By, no "Generated with", no mention of AI/Claude/Anthropic in commits, PR text or code comments.
- Work on branches, never main. User merges PRs.
- No code changes in PART 1. Measure and report only.
- Plugged in, heavy apps closed, nothing else running. Never run two benchmarks at once.
- On a failing command: record the exact error text and move on. Do not silently skip.
- Every claim needs a number from a run.

## Setup (in Ubuntu)
1. `git fetch origin && git checkout feat/phase-a-perf && git pull`
2. `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && python -m pytest -q` (all must pass; tell the user if not, with output).
3. Write `perf/results/local/environment.txt`: OS, kernel/WSL version, CPU model + cores, RAM, `python --version`, `docker --version`, `docker compose version`. Also record the WSL memory/CPU limits (`.wslconfig`) and `docker info` NCPU/MemTotal, since they cap the containers.

## PART 1: measure (no code changes)
A. Baseline vs optimized:
- `git worktree add ../baseline a71e0f4`, copy this branch's `perf/` folder into `../baseline/perf/` (benchmark is new).
- In `../baseline`: `python -m perf.bench --label local-baseline-1` and `--label local-baseline-2`.
- In main folder: `python -m perf.bench --label local-optimized-1` and `--label local-optimized-2`.
- Copy all four result JSONs to `perf/results/local/`.

B. Containers:
- `docker compose --profile load up -d --build`
- `python -m deploy.verify --wait 120 --detect` -> save full output to `perf/results/local/verify.txt` (has a `timings:` line)
- `docker stats --no-stream` -> `perf/results/local/docker-stats.txt`
- `python -m deploy.watch --seconds 60` -> `perf/results/local/watch.txt`
- `docker compose down -v`

C. Detection on the real stack:
- `python -m target.run --kinds all --variants 0,1 --healthy 2 --rows 300000 --summary perf/results/local/live-detect.json` (~15 min)
- `python -m perf.detect_bench --loads 1,4,16 --healthy 1 --label local-detect-vs-load` (~20 min)

D. Write `perf/results/local/REPORT.md`:
- environment
- table baseline vs optimized: req/s, p50, p95, p99, error % at each concurrency level; mean of the two runs and spread between runs
- where CPU goes per service
- container timings (crash-to-recovery, approve-to-healthy, bad-release-to-alert) and container CPU/memory
- detection results (detected x/y, time to detect, false alarms) per fault and per load level
- section "What looks wrong or surprising" (anomalies with evidence)
- section "Top 3 bottlenecks" ranked by evidence
- say plainly what the numbers do NOT prove

E. Commit only `perf/results/local/` on new branch `perf/local-results`, push, tell the user the branch name.

## PART 2: optimize (ONLY after the user says "go" and reviews the report)
- One change per commit. Before each: write hypothesis + expected gain. After: run `python -m perf.bench` twice with a label, compare to the optimized baseline.
- Keep a change only if it beats run-to-run spread by >5% AND `python -m pytest -q` AND `python -m target.run --kinds dependency_outage,bad_deploy --variants 0 --require-detected` pass. Otherwise revert and record why.
- Candidates in evidence order: load generator may be the bottleneck (try `hey`/`wrk` if installed, compare); orders-service makes three sequential DB calls per checkout (batch or parallelize); per-request overhead in `target/common.py` (JSON encode, logging); gateway CPU; Docker CPU/memory limits in docker-compose.yml. Do not touch detector thresholds.
- Finish with a short before/after table and push `perf/local-opt` with a PR description of technical content only.

## Suggested first message to Claude in Ubuntu
"Read PERF_HANDOFF.md in this repo and continue from Setup step 1. Use a Linux clone for benchmarks."
