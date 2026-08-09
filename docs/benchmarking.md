# Benchmarking contract

The benchmark harness will preserve both performance measurements and raw model
output. Each JSONL record must contain enough provenance to interpret it later:

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
