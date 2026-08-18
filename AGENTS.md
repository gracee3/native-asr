# Contributor and agent guidance

`native-asr` owns two offline CPU-first speech workflows: the deterministic
long-form ensemble and the Nemotron-to-Parakeet interactive cascade. Preserve
their provenance, failure, privacy, and protocol boundaries; additional models,
diarization, transcript editing, and synthetic streaming partials are not
implicit roadmap work.

Before changing implementation, read `README.md`, `CONTRIBUTING.md`, and the
affected contract in `docs/architecture.md`, `docs/ensemble.md`, or
`docs/interactive-cascade.md`. Read `docs/models.md`, `docs/benchmarking.md`,
`docs/reproducibility-report.md`, and `docs/licensing.md` for artifact or claim
changes.

## Ordinary validation

```bash
just check
git diff --check
```

These are host-side checks. Uncached Rust dependencies may use ordinary network
access, but do not build images, fetch models or datasets, process audio, access
PipeWire, run benchmarks, or start Docker without separate explicit
authorization.

## Privacy, provenance, and delivery

- Never commit private audio or transcripts, model weights, tokens, local
  benchmark ledgers, source identities, or raw host captures. Public fixtures
  must retain corpus identity, license, immutable revision, digest, and scope.
- Preserve non-root network-disabled inference, read-only model/audio mounts,
  explicit PipeWire source selection, private no-overwrite audit publication,
  and explicit degraded commits.
- Do not compare WER or RTF across materially different segmentation,
  transport, model, or measurement boundaries without saying so.
- Use a focused feature branch. Commit and push the validated change and open a
  pull request; incomplete or higher-risk work stays draft.
- After publication, send the exact commit, PR, validation, outcome, risks, and
  next action to the repository's external coordination record. Do not claim
  completion until that remote handoff is verified.
