# incident-investigator

An AI on-call agent that investigates **simulated** production incidents, finds the root
cause, and proposes a fix that a human must approve. A second layer red-teams the agent with
prompt-injection attacks and measures a guard that blocks them.

> Status: the full codebase is built and tested with fake models. No real-model results yet.
> Numbers will be added only after real runs on the held-out split (see `docs/writeup-template.md`).

## What it will do

1. **Investigate**: read alerts, logs, metrics, deploys and config changes through read-only
   tools and report a root cause with evidence.
2. **Evaluate**: score every run against a hidden answer for each simulated incident.
3. **Approve and replay**: every proposed action waits for a human, and every run is replayable.
4. **Attack and defend** (after the core works): plant injection attacks in the data, measure
   how often the agent obeys, add a guard, and report the drop and the false-block rate.

All incidents are simulated. No real systems or credentials are involved.

## Layout

| Folder | Purpose |
| --- | --- |
| `agent/` | Investigator loop, tools and the model wrapper |
| `shop/` | A simulated backend (10 services) whose telemetry comes from its own state |
| `target/` | A real stack: separate Linux processes with HTTP, SQLite and real faults, watched by the same detector |
| `monitor/` | Anomaly detection, incident grouping, change correlation, recording |
| `gateway/` | Command gateway: allowlisted read-only shell commands for the agent, changes held for approval |
| `readiness/` | Production-readiness checks for Dockerfile, compose, CI workflows, env and secrets |
| `scenarios/` | Hand-generated incidents (`data/`) and monitor-recorded ones (`recorded/`) |
| `evals/` | Scoring harness and reports |
| `guard/` | Attack and defense layer |
| `ui/` | Replay and approval interface |
| `docs/` | Architecture notes and write-up |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
```

## Scenarios and scoring

`scenarios/data/` holds 24 simulated incidents (6 types x 4 variants), each with noisy logs,
metric series, deploys, config changes, a misleading clue and a hidden `answer.json`. They are
generated deterministically by `python -m scenarios.generate`. Variants 0-1 of each type are the
**dev** split (use while tuning); variants 2-3 are **heldout** (use only for final numbers).

```bash
LLM_PROVIDER=ollama LLM_MODEL=llama3.1:8b python -m evals.run --split dev
```

Each run is scored by plain code: root cause correct (category and component), evidence cited,
tool calls used, and wrong blame (a wrong component named with medium or high confidence).
Traces and a `summary.json` are written to `evals/reports/`.

## Monitored app and detection

`shop/` is a small simulated backend: services that call each other (gateway, checkout, auth,
search, databases, cache, payments). Each minute it derives logs and metrics from its own state,
so a fault is a *state change* and the symptoms follow the call chain: a down database makes
checkout log "connection refused" and the gateway's error rate climb; a slow query slows every
caller; a leak fills memory until the container is killed. `shop/faults.py` injects the same six
incident types as the scenario generator, plus decoys (unrelated deploys, red-herring log lines)
and healthy runs with harmless traffic surges.

`monitor/detect.py` watches only metrics and change events. It learns "normal" from the first 30
minutes, flags values far outside it that also rise by a meaningful amount for three minutes in a
row, groups related anomalies into one incident, pages the user-facing service as an SLO alert
would, and lists deploys and config changes shortly before the incident as suspects.

```bash
python -m evals.detect_eval --split heldout          # recall, precision, time to detection
python -m evals.detect_eval --split all --sweep      # quieter and quieter faults
python -m monitor.record                             # save detected incidents as scenarios
python -m evals.run --data scenarios/recorded --split dev
```

A fault counts as detected if an incident is raised between its injection and 30 minutes after
users first feel it; an incident outside every fault window is a false alarm. Time to detection
is measured from first user impact (negative means an early warning, as with memory leaks).

| Split | Faults detected | Precision | False alarms on healthy runs | Median time to detect | Change correlated |
| --- | --- | --- | --- | --- | --- |
| dev | 12/12 | 1.0 | 0/4 | 2 min | 4/4 |
| heldout | 12/12 | 1.0 | 0/4 | 2 min | 4/4 |

These faults are loud, so a clean score is expected and says little by itself. The sweep makes
faults partial (a share of requests fail, queries are only somewhat slow). Detection holds down
to severity 0.25 and drops to 17/24 at 0.1, where faults are nearly invisible. Honest limits:
one hand-set threshold per metric, learned baselines assume the first 30 minutes are healthy,
and a ten-service simulation is far simpler than production.

## Real target app (live processes)

`target/` runs a small stack of real OS processes: `api-gateway`, `orders-service`,
`inventory-service` and `orders-db`, talking HTTP over localhost with a SQLite database. Telemetry
is measured, not derived: request latency and errors from real calls, memory from `/proc`, and a
Prometheus-style `/metrics` endpoint per service. Faults are real: SIGKILL on the database
(`dependency_outage`), a dropped index (`slow_query`), a deploy that ships a genuine
`AttributeError` (`bad_deploy`), a flag file (`feature_flag`), and a leaking thread that a
watchdog OOM-kills (`memory_leak`). Time is compressed: one sample is 0.5 s real, one "virtual
minute".

```bash
python -m target.run --kinds all --variants 0,1 --healthy 1 --out scenarios/live
python -m target.run --kinds dependency_outage,bad_deploy --variants 0 --require-detected   # CI
```

Honest limits: Linux only (reads `/proc`); live runs are not deterministic, so recorded live
incidents are not freshness-checked in CI the way generated ones are; benign deploys are recorded
events, not real restarts; `expired_certificate` is not implemented on the live stack; and it is
four processes on one machine, not a cluster.

## Command gateway

`gateway/` is how the agent looks at a live system. It gets one tool, `run_command`, and every
command goes through a policy before anything runs. The default is deny.

| Verdict | Examples | What happens |
| --- | --- | --- |
| allow | `ps -ef`, `free -m`, `df -h`, `uptime`, `ls logs`, `tail -n 50 x.log`, `grep -i text x.log`, `curl -s http://127.0.0.1:<port>/health` | runs now, output capped at 4000 characters |
| approve | `svc restart <service>`, `svc rollback <service>` | not run; becomes a proposal a human approves or rejects |
| deny | `rm`, `sudo`, `sh`, pipes, redirects, `$(...)`, paths outside the logs folder (including `..` and symlinks), `curl` to any other host or path, unknown services | refused with a reason the model can read |

Commands are split with `shlex` and run without a shell, with a scrubbed environment, a 5 second
timeout and no stdin. Output goes through the same guard as other tool results, so an instruction
planted in a log line is stripped. Every command, decision and approval is kept in an audit list.

```bash
python -m gateway                          # start the real stack, break it, tour the gateway
python -m gateway "free -m" "tail -n 5 api-gateway.log"
```

Honest limits: `ps -ef` shows every process on the host, not only the stack; the allowlist is
small and hand-written; and the investigator has not yet been scored against the live stack with a
real model. This is the safe door the next measurements go through.

### Investigating the live stack

```bash
python -m target.investigate --kinds dependency_outage,bad_deploy --variants 0
```

This breaks the real stack, records what the monitor saw, then leaves the stack running and lets
the investigator use its usual tools plus `run_command` through the gateway. The run is scored
like any other (root cause, evidence, wrong blame) and also reports how many commands ran, how
many were denied, and how many changes were proposed for approval. A test drives this path with a
scripted model (`LIVE_TESTS=1 pytest tests/test_target.py`). No real-model score on the live stack
exists yet; the numbers go here once the run is done.

## Deploying the stack (containers)

The real stack also runs as containers: `docker-compose.yml` starts the gateway, orders service,
inventory service and database from `Dockerfile.stack`. The containers have healthchecks, a
restart policy, memory and process limits, a read-only root, dropped capabilities, and only the
gateway is published (on localhost).

```bash
docker compose up -d --build
python -m deploy.verify --wait 120     # checks, then breaks and repairs the deployment
docker compose --profile load up -d    # optional steady traffic
```

`deploy.verify` checks that all four services are healthy and users can check out. It then
deploys a buggy release (users see errors, and the bug is findable in the logs), rolls it back,
crashes a service (the restart policy must bring it back), and confirms the stack ends healthy.
CI runs this on a real Docker daemon in the `deploy` job. To ship the buggy release by hand, set
`ORDERS_BUG=1` and run `docker compose up -d --no-deps orders-service`.

The same detector and gateway work on the containers. `python -m deploy.watch` scrapes every
service's `/metrics` (all four are published on localhost) and prints alerts. `DockerStack`
lets the command gateway read container logs (copied from `docker compose logs`) and run approved
restarts and rollbacks. `python -m deploy.verify --detect` (run with `--profile load`) also checks
that the detector alerts on the bad release and that the gateway reads logs, refuses `rm -rf /`
and holds a rollback until approved.

Run it on one EC2 instance with `deploy/aws/deploy.sh` (step-by-step guide in `deploy/aws/README.md`; no secrets, SSH from your IP only, services bound to localhost).

Not yet done: an actual cloud run, whose output goes into the write-up. A brief crash that the restart policy fixes in a
second or two is too short for the detector's four-sample rule, so it is not asserted.

## Production-readiness checker

`readiness/` checks that the deployment files are safe and consistent before anything ships. Every
rule has an id, a severity and a fix hint, and each one is tested against a bad and a good example.

```bash
python -m readiness .                 # human-readable
python -m readiness . --json --out evals/reports/readiness.json
python -m readiness . --fail-on high  # exit 1 on any high finding (this is what CI runs)
```

| Area | Examples of what it catches |
| --- | --- |
| Dockerfile (`DF`) | unpinned base image, runs as root, no HEALTHCHECK, secrets in ENV/ARG, `curl \| sh`, `ADD` of URLs |
| docker-compose (`DC`) | unpinned images, no healthcheck / restart policy / memory limit, privileged mode or Docker socket, literal secrets, unknown `depends_on`, datastore ports on all interfaces, undefined volumes, `${VARS}` missing from `.env.example` |
| GitHub Actions (`GH`) | no `permissions`, unpinned or movable-tag actions, `pull_request_target`, secrets echoed, script injection from PR titles, no job timeout |
| Repo (`RP`, `YAML`) | `.env` not git-ignored, weak `.dockerignore`, committed keys (AWS, Groq, GitHub, private keys), invalid YAML |

On this repository it found a root container, no healthcheck and missing job timeouts; those are
fixed. Three low findings remain on purpose: CI actions are pinned to a release tag rather than a
commit SHA. It is a transparent rule set, not a replacement for hadolint or actionlint.

## Approval, replay and the UI

Every proposed action goes through an approval gate: opening a ticket may be auto-approved, and
anything that changes a system (rollback, restart, flag toggle) waits for a human. Actions run
against a simulation only. Runs, proposals and an audit log are stored in SQLite.

```bash
pip install -e ".[ui]"
streamlit run ui/app.py        # run, replay, approve or reject, audit log
```

Or with Docker: `docker build -t incident-investigator . && docker run -p 8501:8501 -v data:/data incident-investigator`

## Attack and defense

`guard/` plants 18 prompt-injection attacks (6 intents x log / alert / deploy message, half
blunt and half paraphrased) in the simulated data and adds a guard: untrusted-data labeling, an
action policy, and a rule-based injection filter. Each part can be switched off to measure it.

```bash
LLM_PROVIDER=... python -m evals.attack_run --split dev --attacks-per-scenario 3
```

It runs four conditions on the same scenarios (clean or attacked, guard off or on) and prints
obeyed injections before and after, accuracy under attack, and what the guard wrongly blocked.

## Baseline

`python -m evals.baseline` runs a rule-based, non-LLM investigator over all scenarios. It is a
floor for comparison and a pipeline check in CI. It is not a model result.

## Windows quick start (local model)

1. Install [Ollama](https://ollama.com/download), then `ollama pull llama3.1:8b`.
2. Install Python 3.11+ from python.org. If `python` opens the Microsoft Store, use `py` instead.
3. In PowerShell, from the project folder:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -e ".[dev]"
Copy-Item .env.example .env          # then set LLM_PROVIDER=ollama and LLM_MODEL in .env
python -m evals.run --split dev --limit 2
python -m evals.show_trace evals\reports\latest\traces\bad_deploy-0.json
```

Settings are read from `.env`, and real environment variables win over it. If a local model
cuts off or ignores its instructions, raise `OLLAMA_NUM_CTX`. Models that print tool calls as
JSON text instead of calling the tool are handled, and the trace marks them as recovered.

## Free hosted model (optional)

A free API tier can replace the local model. Groq is one option with an OpenAI-compatible
endpoint. Create a key in its console, then in `.env`:

```
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=your-key
LLM_MIN_INTERVAL=2.5
```

Free tiers cap requests per minute and per day (check the provider's current limits), so the
client spaces calls by `LLM_MIN_INTERVAL` seconds and retries on rate-limit errors. Keep the key
out of git: `.env` is ignored. Use the same model for every run you compare.

## Choosing a model

The model is set in `.env` and nothing else in the code depends on it:

- `LLM_PROVIDER=mock`: scripted replies, used by the tests.
- `LLM_PROVIDER=ollama`: a local model for development.
- `LLM_PROVIDER=openai_compat`: any hosted OpenAI-compatible API for the final scored runs.

Do not mix results from different models in one comparison.

## Development rules

- Work on a branch and open a pull request. Do not commit to `main`.
- CI runs lint, tests and (later) a small eval on every pull request.
- Never commit secrets. Use `.env`, which is ignored by git.

## License

MIT
