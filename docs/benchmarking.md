# Benchmarking contract

The benchmark harness preserves both performance measurements and raw model
output. Each JSONL record contains enough provenance to interpret it later:

- runtime name and pinned version or commit;
- image identity and size;
- model alias, artifact revision, SHA-256, quantization, and weight size;
- host CPU and thread count;
- original audio path or user-supplied identifier and duration;
- normalization and segmentation strategy;
- wall time, CPU user/system time, peak RSS, and exit status;
- transcript plus timestamp or segment availability;
- benchmark timestamp and tool schema version.

Real-time factor is defined as:

```text
RTF = wall_seconds / audio_seconds
x_realtime = audio_seconds / wall_seconds
```

An RTF of `0.5` is twice realtime; `0.1` is ten times realtime. Both values are
reported because the inverse is easier to scan while RTF is conventional.

## Comparison rules

Raw/native decoding and production long-form VAD/chunking are separate modes.
A result must name its mode and segmentation parameters. Runs are comparable
only when normalization and segmentation are equivalent or the difference is
made explicit.

Accuracy details retain both raw and normalized text. English normalization
policy `english-upper-apostrophe-v1` uppercases text, normalizes apostrophes,
replaces other punctuation with spaces, and collapses whitespace. Aggregate WER
stores substitutions, deletions, insertions, reference words, and failures.

## Measurement boundary

`scripts/benchmark` verifies the model checksum and inspects the image before
starting the clock. Its timed region then includes Docker startup, temporary
16 kHz mono PCM16 normalization, model loading, and inference. This is an
end-to-end user-visible measurement, not a kernel-only inference timer.

GNU `/usr/bin/time` runs inside the image and supplies recognizer CPU time and
peak RSS. Host nanosecond timestamps supply end-to-end elapsed time, including
container startup, and FFprobe supplies source duration. The script appends one
compact record under an exclusive `flock`, including failed runs and their exit
status. The default destination is `/data/benchmarks/native-asr/runs.jsonl`.

`scripts/run-full-benchmark` is the durable public runner for the reviewed
matrix. It executes the nine-alias smoke and subset gates, the eight full-split
finalist cells, the streaming matrix, validation after each phase, and a final
resume audit. It holds an exclusive ledger lock and requests a system sleep
inhibitor while active. `scripts/run-test-clean-pair` provides the narrower
two-model recovery run used during the original benchmark campaign. Both use
the shared external-path defaults and accept the documented `NATIVE_ASR_*`
overrides.

## Public evaluation sets

`scripts/datasets` locks, fetches, verifies, and prepares LibriSpeech
`test-clean`/`test-other` and AMI `ES2004a`. Official archives remain cached;
timed inputs are precomputed 16 kHz mono PCM16 files. JSONL manifests preserve
utterance ID, split, paths, raw reference, duration, speaker, and source digest.

`scripts/benchmark-set` ranks subset candidates by SHA-256 of utterance ID and
scores independent reference/hypothesis pairs with a fingerprinted
runtime-specific batching policy. Most runtimes use one container and one model
load for the set. Sherpa Parakeet uses bounded groups and the pinned Silero VAD
path for utterances over 20 seconds to avoid the upstream offline CLI's large
concurrent-stream and long-utterance failure modes. Detail output is atomic and
prior runs remain. Resume requires identical image, model, dataset,
preprocessing, adapter, and options. A separate single-utterance cold
calibration probe records startup, model load, and first-inference wall time;
it is not included in the batch RTF.

Streaming JSONL uses `stt_partial`, `stt_final`, `stt_error`, and `stt_metrics`
with ordered sequence, audio position, monotonic time, text, finality, and
latency. Sherpa and NeMo expose stateful file decoding but no incremental CLI
callback, so their adapters report zero partials instead of inventing them.
Streaming summaries retain image/model/audio/adapter fingerprints, CPU time,
peak RSS, RTF, failures, and event-order validation. Paced runs report partial
and finalization lag; unpaced AMI runs leave those latency fields null rather
than subtracting media time from throughput time.

The latest engineering and deterministic subset validation, plus the boundary
between completed subset coverage and pending full/streaming stages, are in
[`reproducibility-report.md`](reproducibility-report.md).

The exact records behind the README's validated 100-utterance tables are
versioned in
[`benchmarks/published/2026-08-09-librispeech-100`](../benchmarks/published/2026-08-09-librispeech-100/README.md).
Generated ledgers remain external by default; only reviewed snapshots derived
from public evaluation corpora belong in that directory.
