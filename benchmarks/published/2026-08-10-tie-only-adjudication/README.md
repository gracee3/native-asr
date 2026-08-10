# Tie-only local-LLM adjudication: 2026-08-10

This directory contains the repeated calibration and held-out evaluation for
policy `primary-fallback-only-v1`. Both Ministral candidates ran twice with
fresh tie-only prompts on each snapshot. `results.json` is the combined
schema-2 record; `models/` preserves the two independently captured source
results used by its strict provenance-reuse pass.

| Model | Calibration errors | Fallbacks per repeat | Held-out errors | Fallbacks per repeat | Recommendation |
|---|---:|---:|---:|---:|---|
| Ministral 3B | 37 + 54 = 91 | 1 | 41 + 72 = 113 | 2 | blocked |
| Ministral 8B | 37 + 54 = 91 | 1 | 41 + 72 = 113 | 3 | blocked |

The dynamic deterministic baselines are 37/54 for calibration and 41/72 for
held-out validation. Both models produced identical canonical decisions and
scores across repeats, but neither strictly improved either combined baseline.
Both also failed the zero-fallback gate. The 3B fallbacks were repeatable
protected-boundary conflicts. The 8B result had the same repeatable boundary
conflicts plus one repeatable malformed response on held-out `test-clean`.

The overall recommendation is therefore blocked. Because no candidate passed
both calibration and held-out gates, the conditional 123.95-second long-form
fixture evaluation was not run and no production alias is recommended.

All evaluation records reference clean Git revision
`d52b464b36ab6cf060b464d3ba3b37f9b8724f71`. Calibration used the original
ranks 0–99 snapshot; validation used the reviewed, disjoint ranks 100–199
snapshot. The JSON files retain exact model/runtime provenance, decisions,
fallback reasons, timings, p95 latency, and peak RSS.
