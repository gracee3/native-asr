# Ensemble Reconstruction and Future Scheduling

The deterministic three-model, recorded-audio milestone is now implemented;
see [`ensemble.md`](ensemble.md) for its command and audit contract. This
document records the remaining direction for local-LLM-assisted reconstruction,
concurrent scheduling, persistent workers, and streaming adaptation.

## Objective and trust boundary

The ensemble should improve accuracy by retaining independent ASR hypotheses,
aligning them deterministically, accepting text on which they agree, and using
a local LLM only to adjudicate bounded disagreement regions. The LLM must not
silently rewrite an entire transcript or erase provenance.

The output should retain the raw model hypotheses, the deterministic consensus,
the reconstructed transcript, and the locations and reasons for material
changes. An authoritative final result belongs to the ensemble track; a final
segment from an individual ASR model is final only within that model's track.

## Current host capacity

The validated development host is an Intel Core i7-1185G7 with four physical
cores, eight hardware threads, 12 MiB shared L3 cache, and approximately 31 GiB
of RAM. Although the operating system exposes eight logical CPUs, this is not
equivalent to eight independent physical cores.

The published 2026-08-09 CPU benchmarks show the scheduling pressure:

- Sherpa Canary and whisper.cpp average roughly 7.7 to 7.9 logical CPUs;
- Sherpa Nemotron averages roughly 7.2 logical CPUs;
- Moonshine averages roughly 6.5 logical CPUs; and
- the pinned NeMo-Speech.cpp models use a fixed four-thread graph and average
  roughly 3.8 to 4.0 CPUs.

Peak in-container RSS is about 0.7 to 1.5 GiB for most current models, about
3.5 GiB for Sherpa Parakeet, and about 6.8 GiB for Sherpa Nemotron. Several
selected ASR models can therefore fit in 32 GiB. Steady-state CPU execution,
shared cache and memory bandwidth, and sustained thermals are expected to limit
concurrency before RAM capacity does. Swapping during inference is unacceptable
for predictable long-form or streaming latency.

These figures are evidence from isolated runs, not a substitute for an explicit
concurrency benchmark. They are recorded in the published benchmark snapshots
and must be remeasured if runtimes, adapters, thread settings, or hardware
change.

## Long-form scheduling

Sequential model passes should be the initial, reproducible default. Using the
validated full-split RTFs, Sherpa Parakeet at approximately 0.14 plus NeMo
Parakeet TDT at approximately 0.16 gives a combined sequential RTF near 0.30:
roughly 18 minutes of ASR work for one hour of comparable audio, before
alignment and reconstruction.

Adding whisper.cpp `small.en` provides a valuable different runtime family but
raises the approximate sequential total to 0.91 through 1.12 RTF on the two
validated subsets. Real long-form recordings can behave differently, so these
are planning estimates rather than latency promises.

Controlled two-worker execution should be evaluated as an optional throughput
mode. Two fixed four-thread NeMo workers are the most natural experiment. A
Sherpa or Whisper worker must receive a smaller thread budget before being
co-scheduled because its current eight-thread configuration already consumes
nearly the entire logical CPU. Two workers will share four physical cores,
cache, memory bandwidth, and thermal headroom, so a two-times speedup is not
expected. Unrestricted three-worker CPU inference is not an initial target.

The concurrency benchmark should compare at least:

1. sequential execution with the validated per-runtime thread settings;
2. two four-thread workers;
3. controlled CPU affinity or quotas where the runtime permits them; and
4. three workers only as a boundary measurement.

It should record total wall time, per-worker CPU time, RTF, peak aggregate RSS,
model loads, failures, thermal or frequency observations, and transcript
equivalence. Parallel benchmark results must not be compared with sequential
results without identifying the scheduling profile.

## Persistent model workers

An ensemble job should load each selected model once and keep its inference
process alive for the duration of the recording or batch. Operating-system file
cache can reduce later disk reads, but it does not preserve an initialized
runtime graph, allocated buffers, tokenizer, decoder state, or thread pools.

The orchestrator must therefore avoid starting a new container or CLI process
for every audio segment. Model-load count remains a measured property rather
than an assumption: the current NeMo full-split path reused one load across
2,620 utterances, while Sherpa Parakeet recovery and VAD paths required multiple
processes. Persistent workers may require a batch API, long-lived subprocess,
or later a local service boundary.

Shared audio segmentation is desirable where model contracts allow it because
it simplifies time-aligned comparison. Runtime-native segmentation and
timestamps must still be preserved; hypotheses produced under materially
different segmentation strategies should not be treated as directly aligned
without an explicit reconciliation step.

## Local LLM reconstruction

A modest quantized LLM can fit beside the current ASR models in 32 GiB, but it
will compete for the same CPU execution and memory bandwidth. Exact footprint
and generation speed depend on model size, quantization, context length, and
runtime and must be benchmarked after a model is selected.

The default long-form schedule should be phased:

```text
ASR model passes
    -> deterministic time/word alignment
    -> consensus and disagreement detection
    -> LLM adjudication of bounded disagreement windows
    -> validated final reconstruction
```

The LLM process may remain resident while ASR runs, but should normally be idle
until candidate text is available. Running full-speed LLM generation alongside
full-speed ASR is not expected to improve aggregate throughput on this host.
Pipelining can be evaluated later, but only after the sequential baseline.

The LLM should receive short disagreement spans with limited surrounding
context and candidate provenance. Agreed text should bypass generation. This
reduces prompt processing, generated tokens, context memory, latency, and the
surface area for hallucination. Every LLM change should be attributable to a
source span, and the deterministic consensus must remain available for audit or
fallback.

## Later streaming adaptation

Streaming workers must remain loaded and retain decoder state. Sequential
streaming would mean round-robin inference over each audio chunk, not unloading
and reloading models. The isolated RTFs of several streaming candidates sum to
less than one, but that does not guarantee a safe live deadline once contention,
algorithmic lookahead, audio processing, alignment, jitter, and thermals are
included.

A likely streaming presentation and scheduling model is:

- one primary model publishes provisional text immediately;
- secondary models validate completed speech regions within bounded queues;
- deterministic consensus may revise a committed segment after a short delay;
- LLM adjudication runs only after a silence or final boundary and only for
  meaningful disagreements; and
- slow secondary work is skipped or degraded rather than allowed to build an
  unbounded backlog.

The pinned Sherpa and NeMo command-line adapters currently expose stateful file
decoding but not genuine incremental callbacks. Only Moonshine currently emits
native partial callbacks through the shared adapter. Streaming ensemble work
therefore requires a stable stateful runtime interface before scheduling policy
can be validated.

## Implementation status and next posture

The initial implementation now provides the first three items below. Remaining
work should continue to favor measurable correctness over concurrency:

1. **Implemented:** preserve raw per-model outputs and provenance;
2. **Implemented:** define truthful native timing spans and deterministic alignment;
3. **Implemented:** consensus and disagreement localization without an LLM;
4. add bounded local-LLM adjudication behind an optional flag;
5. measure persistent model loads, sequential RTF, aggregate memory, and change
   provenance; and
6. evaluate two-worker execution only after the sequential path is reproducible.

GPU scheduling, live microphone ensemble inference, and continuous LLM
reconstruction are intentionally outside the initial long-form scope.
