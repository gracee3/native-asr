# LibriSpeech full `test-clean` pair: 2026-08-09

This directory contains the exact successful records behind the initial
full-split checkpoint in `docs/reproducibility-report.md`:

- `runs.jsonl` contains two aggregate records: full
  `librispeech-test-clean` for `sherpa:parakeet-unified-en` and
  `nemo:parakeet-tdt-v3`;
- `details/` contains the corresponding 5,240 per-utterance records, including
  public LibriSpeech references, raw and normalized hypotheses, source digests,
  and WER counts.

Both runs used all 2,620 utterances, clean Git revision
`44f2eafd6a6513617ead992714dab26c120b9bef`, locked model and dataset artifacts,
and `english-upper-apostrophe-v1` normalization. Both completed with zero
failures. The two detail files have SHA-256 digests:

- Sherpa: `4b1df76eb35cf5c86b441fabd7430587d6c934d53590391b23b6ef5c8af25def`;
- NeMo: `db197e36db7da183cd8354c9de27157a5776aa3aa75a596ccac0fdcdee1e3133`.

This is deliberately a two-model `test-clean` checkpoint, not a complete
ranking of the four fixed finalists on both splits. See the
[`reproducibility report`](../../../docs/reproducibility-report.md) for the
host, image IDs, batching policies, rejected pre-fix run, interpretation, and
remaining six full-split cells.

The aggregate records are copied unchanged from the append-only external
ledger. Consequently, each `detail_path` records the original absolute capture
location. Its published counterpart is `details/<run_id>.jsonl` here.

To replay just these two cells after fetching and preparing the locked public
datasets and models, use:

```bash
just bench-test-clean-pair
```

New results go to the external `NATIVE_ASR_BENCHMARKS` ledger and are not
silently mixed into this reviewed snapshot.
