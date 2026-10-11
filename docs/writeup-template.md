# Results write-up (fill in after real runs)

Do not fill any number from memory or from a test double. Copy it from `summary.json`.

## Setup
- Model and exact version:
- Date, temperature (0), max steps:
- Split used for final numbers: heldout (12 scenarios)

## Investigator (no attacks)
| Metric | Count | Rate |
| --- | --- | --- |
| Root cause correct | /12 | |
| Evidence cited | /12 | |
| Wrong blame (medium or high confidence) | /12 | |
| No report (step limit) | /12 | |
| Average tool calls | | |

Rule-based baseline on the same split, for reference: __ /12.

## Attack and defense
| | Guard off | Guard on |
| --- | --- | --- |
| Obeyed injections | /N | /N |
| Root cause correct under attack | /N | /N |

False blocks with the guard on, clean runs: legit report blocked __/12, lines removed __.

## What failed and why
Replay two wrong runs and describe the actual failure.

## Limits
Synthetic data, small sample, one model.
