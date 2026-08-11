# Contributing

Contributions are welcome when they preserve the repository's offline,
CPU-first, provenance-aware design.

## Before changing code

- Keep model weights and private audio outside Git and every Docker build
  context.
- Keep inference containers non-root, model-free, and network-disabled.
- Preserve read-only model and source-audio mounts.
- Do not report synthetic streaming partials or compare WER/RTF runs with
  materially different segmentation without saying so.
- Record new model, dataset, and runtime sources with immutable revisions,
  SHA-256 digests, and license identifiers.

## Validation

Run the repository suite before submitting a change:

```bash
just check
git diff --check
```

Changes to runtime images should also rebuild and inspect the affected image.
Benchmark claims need their host, workload, segmentation, model and adapter
fingerprints, and measurement boundary. Private transcripts or local benchmark
ledgers must not be attached to issues or committed.

## Scope

The supported user workflows are the deterministic long-form ensemble and the
interactive Nemotron-to-Parakeet cascade. New runtimes and experimental UI,
concurrency, multilingual, diarization, or LLM work should remain optional and
must not weaken either workflow's current guarantees.

By contributing, you agree that your contribution is licensed under the
repository's [MIT License](LICENSE).
