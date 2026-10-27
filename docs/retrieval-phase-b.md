# Retrieval (Phase B)

What was built, what was measured, and what the numbers do and do not show. Everything here is
reproducible without a language model: `python -m retrieval.eval --split dev` (or `heldout`).

## What it is

`retrieval/` ranks the evidence of one incident and suggests root cause categories. It adds two
opt-in investigator tools; the default tool set is unchanged, so a with/without comparison is clean.

| Piece | File | Notes |
|---|---|---|
| Tokeniser | `text.py` | keeps identifiers whole *and* split (`NullPointerException`, `new_ranking_model`), light stemming |
| BM25 | `bm25.py` | written out, no dependency |
| Embedders | `embed.py` | `ngram` (offline, character n-grams, a fuzzy lexical matcher, **not** a neural model) and `ollama[:model]` (real embeddings from a local Ollama) |
| Fusion | `fusion.py` | reciprocal rank fusion |
| Documents | `docs.py` | log lines grouped by template with a count and time range; deploys; config changes |
| Evidence ranking | `evidence.py` | `bm25`, `ngram`, `rrf`, `rrf+` (adds timing signals) and `slate()` |
| Knowledge base | `kb.py` | runbook entries and past incidents, scored per category by the best matching entry |
| Tools | `tools.py` | `find_evidence(query)` and `suggest_hypotheses()` |

Use them in a run with `python -m evals.run --split dev --retrieval`.

### Design decisions, each from a failure

From the generated incidents (dev split):

- **Group repeated log lines.** 33 identical errors would otherwise fill the whole result list.
- **A slate, not one list.** Root-cause evidence lives in logs, deploys and config changes. The alert
  text says nothing about a deploy, so one mixed ranking let log lines crowd them out. `slate()`
  returns the best few logs, then the deploy and config change that best fit the alert's timing.
- **Best entry per category, not a sum of votes.** With one same-kind past incident per category, a
  category with two weak entries outvoted the one strong match (top-1 fell to 25%).

From the real stack (live variants 0-1; the first run there scored far below the generated data):

- **The alert fires at the front door, the cause is one hop away.** The gateway raises the alert
  while the fault is on the orders service, the database or the inventory service, and a query built
  from the alert text is biased toward the gateway. Half of the slate's log slots now go to the
  biggest bursts of new errors and warnings at the alert's start, on any service. A real symptom
  repeats hundreds of times; a stray unrelated line appears once.
- **The incident signature uses those bursts, not every message in the window.** Decoy lines near the
  alert time were pulling the runbook match toward the wrong category.
- **Deploy timing outweighs service name.** A deploy two minutes before the alert is stronger
  evidence than one on the alerting service from half an hour earlier.

## Results (k=5, `retrieval/results/`)

Evidence: does what was returned contain the keys that identify the root cause? Coverage is the
mean share of an incident's evidence keys found. "Slate" is the logs + deploy + config mix (BM25 for
the query-driven logs).

| | generated dev (12) | generated held-out (12) | **real stack, variants 2-3 (10)** |
|---|---|---|---|
| tail of the log (what `search_logs` returns with no query) | 0.33 | 0.58 | 0.40 |
| tail of errors and warnings | 0.67 | 0.67 | 0.70 |
| BM25 over logs only | 0.67 | 0.67 | 0.65 |
| **slate (chosen)** | **0.96** | **1.00** | **1.00** |
| slate, RRF of BM25 and n-gram | 0.96 | 1.00 | 0.90 |

Root cause category ranking, top-1 / top-3:

| | generated dev | generated held-out | **real stack, variants 2-3** |
|---|---|---|---|
| runbook only | 100% / 100% | 100% / 100% | **80% / 100%** |
| past incidents only | 100% / 100% | 100% / 100% | 80% / 100% |
| runbook + past incidents fused | 100% / 100% | 100% / 100% | 90% / 100% |
| a kind with no past incident, past incidents only | 0% / 0% | 0% / 0% | 0% / 0% |
| same, runbook + past incidents fused | 25% / 75% | 33% / 75% | 60% / 80% |
| chance | 17% / 50% | 17% / 50% | 17% / 50% |

The real-stack column is the honest one: it is real processes writing real logs, with past incidents
taken from the generated set, so retrieval has to generalise from different wording. Variants 0-1 on
the real stack (`live-variants01-all.json`) were used to find the three fixes above, so their numbers
(0.80 slate coverage, 80% runbook top-1) are tuned-on. Variants 2-3 were recorded before the fixes were
made and scored once afterwards. The generated held-out split had been looked at before these changes;
it was re-run to check for regressions and is no longer a pure hold-out.

## What this does not show

- **Ten real incidents is small, and variants 2-3 are not a new world.** Same stack, same five fault
  kinds, same decoy generator, different parameters and timing. One incident moves a number by 10
  points. It shows the fixes were not specific to the ten incidents they came from, not that retrieval
  works on any production system.
- **The runbook shares vocabulary with the logs.** Perfect scores on generated incidents say the
  plumbing works. On real logs the runbook is right first 80% of the time, wrong first for two of ten,
  and always has the true category in the top three.
- **Where retrieval still struggles: ranking inside the slate.** The mean reciprocal rank of the first
  item containing a key is only about 0.43 on real incidents. The right evidence is in the slate, but
  usually not first.
- **A plain baseline is close on logs alone.** The last five errors and warnings score 0.70 on real
  incidents. The slate wins because it adds the deploy and config change and the burst slots.
- **The fused ranking did not beat BM25** for evidence. The n-gram ranker is weak on short alert
  text and dragged fusion down. I kept BM25 because dev said so, and report the fusion rows anyway.
- **Fusing history with the runbook scored as well or better on real incidents** (90% against 80%),
  contrary to the generated data. The difference is one incident, and fused ranking is the part that
  poisoned history breaks (below), so the tool still ranks from the runbook and shows history apart.
- **Past incidents cannot tell a good match from a spurious one.** Similarity scores overlap too much
  for any threshold to work, so no threshold gate is used.
- **No neural embeddings were measured.** Hugging Face is unreachable from the build sandbox. The
  `ollama` embedder is implemented and unit-tested against a mock server but has not been run. To
  measure it: `ollama pull nomic-embed-text`, then
  `python -m retrieval.eval --split dev --embedder ollama:nomic-embed-text --label nomic`.
- **No language model was involved.** Whether the tools help the investigator is a separate question,
  answered by `python -m evals.run --split dev --retrieval` against the same run without the flag.

To reproduce the real-stack numbers:

    python -m target.run --kinds all --variants 2,3 --out recorded-live --workdir .runtime --rows 300000
    python -m retrieval.eval --split heldout --data recorded-live --label live-variants23

## Poisoned history (`python -m evals.history_poison`, no model needed)

The past-incident entries come from earlier incidents, so a planted or corrupted entry is a new way
to mislead the investigator. I measured six plants against all 24 incidents and every wrong label
(120 runs each; `retrieval/results/history-poison.json`):

| Plant | First "similar past incident" shown | Runbook lead changed | Injected sentence reached the model |
|---|---|---|---|
| copy of the incident's own signature, wrong label | **100%** (the tool flags every one as disagreeing with the runbook) | 0% | 0% |
| same, plus a prompt-injection sentence | 92%, flagged | 0% | 0% |
| only the incident's alert wording, wrong label | 8% | 0% | 0% |
| a random 25% of the signature's words, wrong label | 4% | 0% | 0% |
| a random 50% of the words | 42% | 0% | 0% |
| a random 75% of the words | 83% | 0% | 0% |

- **Prompt injection through history is closed by construction, not by filtering.** The tool prints
  a past incident's id and label, never its text, so a sentence planted in the text has nowhere to
  go. A test checks the detector would fire if the text did appear.
- **Label poisoning works against ranking.** The strongest plant wins the similar-incident list
  every time. A knowledge base that fused runbook and history into one ranking fell from 100% to 0%
  top-1 under that plant. That is why the runbook decides the lead and history is shown separately.
- **The defense is a disagreement flag, and it is only a flag.** Each past incident whose label
  differs from the runbook's lead is marked "(disagrees with the runbook's top lead)". Whether a
  model is misled anyway, or whether the flag makes it more cautious, needs a run with a model; this
  measurement stops at what the tool prints.
- **How well the plant works depends on how much the attacker knows.** Knowing only the alert
  wins the list 8% of the time; knowing half of what the logs say, 42%; three quarters, 83%. A fused
  runbook-plus-history ranking falls from 100% to 96%, 58% and 17% top-1 at 25%, 50% and 75%. Every plant that wins the list is flagged, and none moves the runbook.
  Words were chosen at random with a fixed seed, so a smarter choice of words could do better.
- **Where it matters most:** the runbook is hand-written here. If the runbook were also editable
  by whoever writes the history, the flag would protect nothing.

## Remaining question for Phase C

What a model does with a poisoned-but-flagged lead is not measured yet. It needs the 18 existing
attacks and these plants run through a real model, with and without `--retrieval`.
