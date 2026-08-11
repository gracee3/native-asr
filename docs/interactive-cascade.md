# Interactive Nemotron to Parakeet cascade

This document is the authoritative design and acceptance contract for the
interactive `native-asr` path. The sole current development and acceptance host
is the ThinkPad T14 with an Intel Core i5-1145G7, 15 GiB of RAM, and CPU-only
inference. Measurements from the earlier i7-1185G7 host remain reproducible
historical snapshots; they are not acceptance evidence for this cascade.

## Status

The direction and interfaces below are approved. Implementation and T14 gates
are pending until explicitly marked complete in this document and in
`docs/reproducibility-report.md`. In particular, the pinned NeMo CLI's existing
streaming-file adapter still emits no genuine partials and is not evidence that
the cascade's streaming adaptation is complete.

## Product boundary

The English interactive path is a two-pass cascade:

1. `nemo:nemotron-streaming-en` stays resident, consumes 16 kHz mono float PCM,
   and emits provisional hypotheses plus endpointed phrase finals.
2. Each finalized phrase is sent to a resident `nemo:parakeet-tdt-v3` worker.
   A successful correction received before its deadline is authoritative.
3. Nemotron text commits with `degraded=true` when Parakeet fails, returns empty
   text, is unavailable because the queue is full, or misses the 2.5 second
   correction deadline.

The deterministic three-model recorded-audio ensemble remains a separate
offline feature. It is not called from the interactive critical path.
Nemotron 3.5 is reserved for a later multilingual milestone. Rust UI work,
local-LLM adjudication, diarization, GPU execution, and additional full-corpus
benchmarking are out of scope.

## Runtime shape

The existing `asr-nemo-speech` image packages the pinned stable NeMo-Speech.cpp
C ABI, a small native model worker, and a native supervisor. The final image
remains non-root, model-free, compiler-free, source-free, and Python-free. Model
trees are host-managed and mounted at `/models:ro`; every run uses
`--network none`.

The supervisor owns two child processes. Nemotron is persistent for the session.
Parakeet is persistent when healthy and restartable after a crash or protocol
failure. Microphone capture remains a host concern: no container receives
PortAudio or PipeWire device access. The default live command selects a
PipeWire source on the host and pipes normalized samples over standard input.
The deterministic file command uses the same input protocol with optional
real-time pacing. Neither path saves raw microphone or normalized PCM.

Nemotron uses 160 ms right context, word timing, genuine incremental results,
and token-silence endpointing. The endpoint silence threshold defaults to
800 ms and is configurable. End-of-input flushes any nonempty final region.

## Correction scheduling

At most one Parakeet correction is active and one finalized phrase is waiting.
Nemotron continues accepting audio while corrections run. A third finalized
phrase cannot enter the correction queue and therefore commits from Nemotron
with degradation reason `queue_overload`.

Commits always advance in segment order. Later corrections may finish early but
wait behind earlier segments. A timeout, worker failure, empty result, or queue
overload releases the affected segment with Nemotron text. Results arriving
after timeout or cancellation are discarded and can never revise committed
text. The 2.5 second deadline is measured from phrase finalization, not from the
time the Parakeet worker becomes available.

## Event protocol

The machine interface is newline-delimited JSON. Each canonical event contains:

- `sequence`: contiguous, session-global integer starting at zero;
- `monotonic_ms`: milliseconds from the session monotonic origin;
- `session_id`, `track_id`, and integer `segment_id`;
- `revision`: segment-local integer that increases on replacement;
- `state`: `provisional`, `model_final`, or `committed`;
- `audio_start_ms` and `audio_end_ms`;
- `model`: runtime-qualified provenance for the text;
- `latency_ms`: nonnegative latency from the represented audio boundary;
- `text`;
- `degraded` and `degradation_reason`.

Provisional events belong to Nemotron. A Nemotron `model_final` closes the
audio region and creates a correction job. A successful Parakeet
`model_final` replaces that revision. Exactly one `committed` event follows for
each finalized segment. The default terminal viewer renders replacement in
place; `--jsonl` prints only canonical JSONL and diagnostics stay on stderr.

Cancellation stops capture and workers, flushes no invented text, and exits
nonzero unless normal end-of-input had already completed. Capture failure is a
session failure. No event may claim a Parakeet commit without a nonempty,
on-time Parakeet result.

## Optional audit bundle

Persistence is disabled by default. `--audit DIR` requires that `DIR` not exist.
The implementation stages a sibling directory with mode `0700`, writes private
files, fsyncs completed content, and publishes by same-filesystem rename only
after a successful session. It never overwrites an existing path.

A successful audit contains:

- `events.jsonl`, the exact canonical event stream;
- `result.json`, session configuration, provenance, outcome, and aggregate
  metrics;
- `transcript.txt`, text from successful `committed` events in segment order.

Raw audio, normalized PCM, and microphone material are never included. Failed
or cancelled sessions do not publish a successful audit directory.

## Commands

The intended user interface is:

```bash
scripts/cascade live [--source PIPEWIRE_NODE] [OPTIONS]
scripts/cascade file AUDIO [OPTIONS]
just cascade-live
just cascade-file AUDIO
```

Live capture is never invoked by repository checks or automated acceptance.
Public audio with at least one second of silence between utterances is used for
paced interactive validation.

## Acceptance gates

Repository tests cover event ordering, revision replacement, authoritative
commit ordering, endpoint flush, cancellation, capture failure, Parakeet
failure, empty output, timeout, overload, late-result rejection, audit modes,
and no-overwrite behavior.

The final image must again pass the non-root, model-free, compiler/source/Python-
free, resolved-library, read-only-model, and network-disabled inspections. Real
T14 sessions must show one load per model in a healthy run, continuing Nemotron
decode while Parakeet corrects, and no swap growth attributable to peak resident
memory.

Both deterministic 100-utterance LibriSpeech subsets must use at least one
second of silence between utterances and report Nemotron WER, committed WER,
correction rate, transcript churn, degradation count, partial lag, correction
lag, RTF, CPU, peak RSS, and thermals. Acceptance requires contiguous events,
zero runtime failures or degraded segments in nominal paced runs, p95 partial
lag at most 750 ms, every correction committed within 2.5 seconds, and committed
WER no worse than Nemotron on either subset.
