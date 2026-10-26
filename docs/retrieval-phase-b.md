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

### Design decisions, each from a failure seen on the dev split

- **Group repeated log lines.** 33 identical errors would otherwise fill the whole result list.
- **A slate, not one list.** Root-cause evidence lives in logs, deploys and config changes. The alert
  text says nothing about a deploy, so one mixed ranking let log lines crowd them out. `slate()`
  returns the best few logs, then the deploy and config change that best fit the alert's timing.
- **"New near the alert" signal.** A log message that first appears just before the alert, on any
  service, is a strong lead. This is what finds a slow query on the database when the alert fires on
  the service in front of it. It uses only the alert and the timestamps.
- **Best entry per category, not a sum of votes.** With one same-kind past incident per category, a
  category with two weak entries outvoted the one strong match (top-1 fell to 25%).

## Results (`retrieval/results/ngram-{dev,heldout}.json`, k=5, 12 incidents each)

Evidence: does what was returned contain the evidence that identifies the root cause?

| | dev coverage | held-out coverage | held-out all-keys |
|---|---|---|---|
| tail of the log (what `search_logs` returns with no query) | 0.33 | 0.58 | 0.50 |
| tail of errors and warnings | 0.67 | 0.67 | 0.50 |
| BM25 over logs only | 0.67 | 0.67 | 0.50 |
| **slate, BM25 for logs (chosen)** | **0.92** | **0.83** | **0.75** |
| slate, RRF of BM25 and n-gram | 0.67 | 0.67 | 0.58 |
| slate, RRF plus timing signals | 0.83 | 0.75 | 0.67 |

Root cause category ranking (top-1 / top-3):

| | dev | held-out |
|---|---|---|
| runbook only | 12/12, 12/12 | 12/12, 12/12 |
| past incidents only | 92%, 92% | 100%, 100% |
| a new kind of failure that no past incident covers, past incidents only | 0%, 0% | 0%, 0% |
| same, past incidents plus runbook | 8%, 42% | 25%, 42% |
| chance | 17%, 50% | 17%, 50% |

## What this does not show

- **The data is generated, and the runbook shares its vocabulary.** The runbook says `x509`,
  `OOMKilled`, `Seq Scan`; the generated logs say exactly that. A 100% top-1 here is a property of
  this benchmark, not evidence about real systems. The honest reading is that the plumbing works.
- **A simple baseline is close on logs.** Taking the last five errors and warnings scores 0.67
  coverage, the same as BM25 on logs only. The retrieval gain is in the slate: pulling in the
  deploy and config change, which the default tool does not surface without being asked.
- **The fused ranking did not beat BM25.** The n-gram ranker is weak on short alert text, so fusing
  it dragged results down. I kept BM25 for logs because the dev split said so, and report the
  fusion rows anyway. Fusion may help once a neural embedder is in the mix (below); that is untested.
- **Past incidents cannot tell a good match from a spurious one.** The best same-category
  similarity on dev goes as low as 0.47 while the best wrong-category similarity goes as high as
  0.71, so no similarity threshold separates them. For a failure type with no history, a naive
  combined knowledge base is misled. So `suggest_hypotheses` ranks from the runbook, and lists
  similar past incidents separately, labelled "the cause may differ".
- **Twelve incidents per split is small.** Differences of one incident move a number by 8 points.
- **No neural embeddings were measured.** Hugging Face is unreachable from the build sandbox. The
  `ollama` embedder is implemented and unit-tested against a mock server but has not been run.
  To measure it: `ollama pull nomic-embed-text`, then
  `python -m retrieval.eval --split dev --embedder ollama:nomic-embed-text --label nomic`.
- **No language model was involved.** Whether the tools help the investigator is a separate question,
  answered by `python -m evals.run --split dev --retrieval` against the same run without the flag.

## Security note (for Phase C)

The past-incident entries are text derived from earlier incidents' logs, and tool output is
untrusted. Poisoning that history is a new attack path: a planted instruction in a past incident
reaches the model through `suggest_hypotheses`. Tool output already goes through the guard; whether
that holds for this path is a Phase C measurement, not something assumed here.
