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
| `scenarios/` | Simulated incidents and hidden answers |
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
