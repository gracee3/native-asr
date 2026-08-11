# Roadmap

`native-asr` has two supported CPU workflows:

1. the deterministic three-model ensemble for recorded audio; and
2. the persistent Nemotron-to-Parakeet cascade for interactive English speech.

Roadmap work preserves their provenance, privacy, deterministic failure
semantics, and headless command interfaces. Existing locked model aliases stay
available for reproducibility and direct use, but model-catalog expansion,
diarization, and LLM adjudication are not planned.

## Release gate

The T14 remains the sole acceptance host for `v0.1.0`. The release requires
both deterministic 100-utterance LibriSpeech fixtures to pass through an
isolated PipeWire virtual source, using the real live-capture path without
activating a physical microphone. File replay remains useful regression
evidence but cannot substitute for that gate.

Acceptance artifacts, audio fixtures, event streams, and audit bundles remain
external to Git. Only reviewed aggregate configuration and results may be
committed. Any runtime, model, endpointing, capture, or host change requires the
two loopback fixtures to be repeated before another release claim.

## Completed post-release milestone: streaming-first Rust TUI

The first post-release milestone delivered a small Rust terminal client for the
existing canonical JSONL protocol. It provides:

- explicit session start and stop;
- PipeWire source enumeration and deliberate source selection;
- visibly distinct provisional and committed text;
- current latency and degradation status; and
- optional audit-bundle selection, disabled by default.

The TUI is a client, not another inference runtime. It does not embed ASR,
rewrite or edit transcript text, invoke an LLM, infer speakers, or perform
diarization. The headless `scripts/cascade` interface remains supported and is
the source of truth for event and failure semantics.

## Measurement backlog

- Measure the complete three-model ensemble end to end on the current T14,
  including verification, all model passes, alignment, publication, aggregate
  RSS, CPU, and thermals. The current 0.91-1.12 RTF is a historical i7 planning
  estimate, not a T14 end-to-end result.
- Extend public long-form validation beyond deterministic 100-utterance
  snapshots without silently changing segmentation policy.
- Measure controlled two-worker long-form scheduling only after a clean
  sequential T14 baseline; shared CPU, cache, bandwidth, and thermals make a
  speedup uncertain.
- Add richer truthful progress events where a runtime exposes useful progress.

## Outside active planning

Additional models, diarization, LLM adjudication, transcript editing, GPU
execution, web synchronization, reconnectable background services, and
unrestricted multi-model concurrency are not part of the current or next
milestone.
