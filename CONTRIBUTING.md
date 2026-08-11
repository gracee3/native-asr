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

`scripts/check` formats the Rust crate, runs Clippy with warnings denied, and
runs the locked Rust unit, process, UI-backend, and pseudo-terminal tests. Keep
`tui/Cargo.lock` and `tui/rust-toolchain.toml` in sync with intentional Rust
dependency or toolchain changes.

Changes to runtime images should also rebuild and inspect the affected image.
Benchmark claims need their host, workload, segmentation, model and adapter
fingerprints, and measurement boundary. Private transcripts or local benchmark
ledgers must not be attached to issues or committed.

## Scope

The supported user workflows are the deterministic long-form ensemble and the
interactive Nemotron-to-Parakeet cascade. Existing aliases remain supported,
but additional models, diarization, and LLM adjudication are not active roadmap
work. Experimental concurrency or multilingual changes must not weaken either
workflow's current guarantees. The Rust TUI remains a live-only client of the
headless cascade protocol; file browsing and transcript editing do not belong
in that boundary.

By contributing, you agree that your contribution is licensed under the
repository's [MIT License](LICENSE).
