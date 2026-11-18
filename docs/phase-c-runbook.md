# Phase C runbook: running the investigator with a real model

Phases A and B use no model. Phase C is the first time a language model investigates incidents, so
it is where the headline numbers come from. This page says what is being measured, in what order to
run it, and what to send back.

## What is being measured

**Investigation quality** (`evals.run`). For each recorded incident the model gets read-only tools,
investigates, and writes a report. Plain code (`evals/score.py`) compares the report with the hidden
answer and records four yes/no results per incident:

| Result | Meaning |
| --- | --- |
| `root_cause_correct` | the report names the true cause |
| `evidence_cited` | it cites the log lines or change that prove it |
| `wrong_blame` | it blames the wrong thing (a confident mistake, worse than "unsure") |
| `action_correct` | the action it proposes is the right one |

Run it twice: once with the default tools, once with `--retrieval` (adds the search tools from
Phase B). The difference between the two runs is the measured benefit of retrieval.

**Attack and defense** (`evals.attack_run`). Prompt-injection text is planted in logs or tool
output, and the run counts how often the model obeys it. Four conditions are run:

| Condition | Attacks | Guard | Why |
| --- | --- | --- | --- |
| `clean_off` | no | off | reference accuracy |
| `attack_off` | yes | off | the "before" number |
| `attack_on` | yes | on | the "after" number |
| `clean_on` | no | on | what the guard wrongly blocks |

## Rules

- Use the **dev** split only. Do not run `--split heldout` or `--split all` until you decide the
  settings are final; the held-out split is the honest test and can be used once.
- Never paste or commit your API key. `.env` is git-ignored.
- A model call costs time. Free tiers have daily limits; `evals.run` can continue with `--resume`.

## Steps (Linux, from the repo folder)

1. Update and activate the environment.

   ```
   git switch main && git pull
   source .venv/bin/activate
   ```

2. Stop the containers (they are not needed for these runs).

   ```
   docker compose down
   ```

3. Check the model settings without showing the key, and send me the output.

   ```
   grep -v API_KEY .env
   ```

   Expected for Groq: `LLM_PROVIDER=openai_compat`, `LLM_BASE_URL=https://api.groq.com/openai/v1`,
   `LLM_MODEL=openai/gpt-oss-120b`, `LLM_MIN_INTERVAL=2.5`.

4. Smoke test: two incidents with retrieval, to confirm everything is connected.

   ```
   python -m evals.run --split dev --retrieval --limit 2 --out evals/reports/smoke
   grep -c find_evidence evals/reports/smoke/traces/*.json
   ```

   The second command counts how often the model used the new search tool. A number above 0 in
   the traces means retrieval was available and used.

5. Full dev run without retrieval, then with retrieval.

   ```
   python -m evals.run --split dev --out evals/reports/dev-plain
   python -m evals.run --split dev --retrieval --out evals/reports/dev-retrieval
   ```

   If a daily limit stops a run, run the same command again with `--resume` added.

6. Only after steps 4 and 5 look sensible: the attack run (dev split).

   ```
   python -m evals.attack_run --split dev --out evals/reports/attack-plain
   python -m evals.attack_run --split dev --retrieval --out evals/reports/attack-retrieval
   ```

## What to send back

The summary table each command prints at the end (copy the text; it contains no key). That is
enough to fill the Results page and the write-up. If a command fails, send the last 20 lines of
its output.
