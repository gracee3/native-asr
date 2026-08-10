# Bounded adjudicator bake-off (blocked)

This snapshot records the 2026-08-10 candidate-selection-only adjudicator
bake-off over the committed 200-utterance LibriSpeech ensemble snapshot. The
snapshot contains 141 disagreement spans and 244 non-unanimous columns. Both
models ran twice through the pinned llama.cpp `b10333` CPU runtime with four
threads, one slot, greedy decoding, and a 30-second acceptance deadline per
span.

Publication is blocked. Both models repeated their validated decisions and
scores exactly, but neither met the accuracy gates. No model is recommended,
so the recommended-profile long-form repeats and draft PR publication were not
run.

## Accuracy and determinism

| Alias | `test-clean` errors | `test-other` errors | Combined | Valid / fallback spans per repeat | Repeat decisions | Qualifies |
|---|---:|---:|---:|---:|---|---|
| `llm:ministral-3b-instruct-2512` | 40 / 2,084 | 60 / 1,645 | 100 | 132 / 9 | identical | no |
| `llm:ministral-8b-instruct-2512` | 45 / 2,084 | 57 / 1,645 | 102 | 136 / 5 | identical | no |

The deterministic baseline is 37 `test-clean` errors and 54 `test-other`
errors, or 91 combined. Qualification requires no split to exceed its baseline
and requires a strict combined improvement. Selection-only oracle ceilings are
27 and 40 errors.

## CPU measurements

| Alias | Load seconds | Prompt tok/s | Generation tok/s | Span p50 / p95 seconds | CPU seconds | Peak RSS GiB |
|---|---:|---:|---:|---:|---:|---:|
| `llm:ministral-3b-instruct-2512` | 0.994, 1.087 | 17.13 | 10.05 | 10.37 / 18.95 | 7,161.58, 7,208.84 | 5.82 |
| `llm:ministral-8b-instruct-2512` | 3.787, 4.092 | 13.85 | 5.89 | 12.73 / 23.31 | 8,982.04, 8,990.55 | 12.44 |

The host was an Intel Core i7-1185G7 (4 cores / 8 threads, 31 GiB RAM). Each
repeat issued all 141 requests to one persistent, network-isolated worker. Late
responses were discarded after the acceptance deadline and drained only to
preserve JSONL alignment and whole-process resource accounting.

## Evidence

[`results.json`](results.json) is the complete schema-1 benchmark result. It
includes the snapshot digest, locked model/runtime provenance, every validated
decision and fallback, repeat decision SHA-256 digests, split scores, request
counts, and per-repeat execution measurements. Its SHA-256 is
`5b6393a0ca2f85966238f749a1af3cdcc8368b2471ad364cf4ba48f7aa7fdbe1`.

The 3B repeat records were reused from an immediately preceding
telemetry-complete run. The combined result records that source digest and
revalidated the snapshot shape, repeat count, model digest, runtime revision,
scores, decision identity, and cross-model gate.
