# LibriSpeech 100-utterance snapshot: 2026-08-09

This directory contains the exact successful records behind the validated
nine-model table in `docs/reproducibility-report.md`:

- `runs.jsonl` contains 18 aggregate records: nine model aliases on each of
  `librispeech-test-clean` and `librispeech-test-other`;
- `details/` contains the corresponding 1,800 per-utterance records, including
  public LibriSpeech references, raw hypotheses, normalized hypotheses, source
  digests, and WER counts.

Every run used the first 100 utterance IDs ranked by SHA-256, clean Git revision
`797eb65c3216702457b551f9308125203cc2b331`, locked model and dataset artifacts,
and `english-upper-apostrophe-v1` normalization. All 18 runs completed with zero
failures. See [`docs/reproducibility-report.md`](../../../docs/reproducibility-report.md)
for the host, image IDs, interpretation, and limitations.

The aggregate records are copied unchanged from the append-only external
ledger. Consequently, each `detail_path` records the original absolute capture
location. Its published counterpart is `details/<run_id>.jsonl` in this
directory.

To rerun one cell after fetching and preparing the locked public datasets and
model, use:

```bash
./scripts/benchmark-set --limit 100 \
  sherpa:parakeet-unified-en librispeech-test-clean
```

Repeat that command for the nine aliases and two LibriSpeech splits recorded in
the [`reproducibility report`](../../../docs/reproducibility-report.md) to
recreate the full 18-cell gate. New results go to the external
`NATIVE_ASR_BENCHMARKS` ledger and are not silently mixed into this reviewed
snapshot.
