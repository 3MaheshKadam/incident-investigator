# Architecture

```
alert ──► investigator loop ──► read-only tools ──► simulated evidence
              │   ▲                    │
              │   └── guard (optional)─┘   labels tool output as data, removes instruction-like
              │                            lines, checks the final report against a policy
              ▼
        structured report ──► approval gate ──► simulated system
              │                    │
              ▼                    ▼
        trace + audit log (SQLite)  human approve / reject
              │
              ▼
        replay view (Streamlit)  ·  scoring harness (evals/)
```

## Parts

| Path | Role |
| --- | --- |
| `agent/llm.py` | One interface over mock, Ollama and any OpenAI-compatible API. Chosen by `LLM_PROVIDER`. |
| `agent/tools.py` | Four read-only evidence tools and the `submit_report` schema. |
| `agent/investigator.py` | The loop: step limit, trace, optional guard hooks. |
| `agent/remediation.py` | Fixed action set, simulated system, approval gate. |
| `agent/store.py` | SQLite for runs, proposals and the audit log. |
| `scenarios/generate.py` | Deterministic generator for 24 incidents with noise and red herrings. |
| `evals/score.py`, `evals/run.py` | Scoring against hidden answers; dev and held-out splits. |
| `evals/baseline.py` | Rule-based, non-LLM floor. Also the CI smoke test. |
| `guard/guard.py` | Labeling, action policy, injection filter. Each part can be switched off. |
| `guard/attacks.py` | 18 injection attacks planted in logs, alerts and deploy messages. |
| `evals/attack_run.py` | Four-condition before/after measurement. |
| `ui/` | Replay and approval interface. |

## Design rules

- The agent only reads. Proposed actions come from a fixed set and run against a simulation.
- Anything that changes a system waits for a human. Opening a ticket may be auto-approved.
- `answer.json` is never opened by agent code, and a test checks the answer cannot leak.
- Results come from the held-out split only. The dev split is for tuning.
- Report counts (for example 17/24), not only percentages.

## Known limits

- All data is synthetic. This shows engineering and evaluation skill, not production experience.
- 24 scenarios is small: a few points of difference is noise.
- Root cause is scored by matching a fixed category and component, which makes scoring exact but
  leaves out free-text explanations.
- The injection filter is rule-based and misses paraphrased attacks on purpose. The measured
  numbers show how much each guard part helps.
- A proposed action that matches an attacker's request counts as obeyed even though a human would
  still have to approve it.
