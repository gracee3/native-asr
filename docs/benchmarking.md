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

Transcripts stay raw for side-by-side accuracy review. A future WER layer may
derive normalized evaluation text, but it must retain both the original model
output and the normalized text. Private recordings, transcripts, and JSONL
results are ignored by Git by default.

## Measurement boundary

`scripts/benchmark` verifies the model checksum and inspects the image before
starting the clock. Its timed region then includes Docker startup, temporary
16 kHz mono PCM16 normalization, model loading, and inference. This is an
end-to-end user-visible measurement, not a kernel-only inference timer.

GNU `/usr/bin/time` runs inside the image and supplies recognizer CPU time and
peak RSS. Host nanosecond timestamps supply end-to-end elapsed time, including
container startup, and FFprobe supplies source duration. The script appends one
compact record under an exclusive `flock`, including failed runs and their exit
status. The default destination is `benchmarks/runs.jsonl`.
