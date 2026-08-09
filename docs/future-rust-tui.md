# Future Rust TUI

This document records a possible user interface direction. It is deliberately
future work: it does not change the current CPU-first runtime, ensemble scope,
command-line behavior, or benchmark plan.

## Product boundary

The TUI should be a client of a headless transcription engine. Inference,
ensemble alignment, reconstruction, durable output, and model provenance remain
outside the terminal application. Existing non-interactive commands must keep
working without a terminal.

The initial implementation should spawn the engine and consume a versioned
newline-delimited JSON event stream. Direct Rust FFI to every ASR runtime is not
an initial requirement. A local socket or reconnectable job service can be
considered later without changing the presentation model.

## Shared event interface

Offline and streaming transcription should eventually expose the same event
envelope. Each event needs a schema version, session ID, ordered sequence
number, event type, and monotonic emission time. Machine-readable events belong
on stdout; diagnostics belong on stderr.

The interface should be able to represent:

- session start, advertised capabilities, phase changes, and honest progress;
- provisional, final, revised, and superseded transcript segments;
- warnings, errors, metrics, cancellation, and successful completion;
- independent raw-model, consensus, and reconstructed transcript tracks; and
- source-audio time ranges when the runtime provides them.

Transcript updates need stable `track_id` and `segment_id` values plus an
explicit revision and state. A model-final segment is final only within that
model's track; it must not be confused with the authoritative ensemble result.
Unknown fields and event types should be safely ignorable so minor protocol
extensions do not break an older TUI.

Offline runtimes do not always expose granular progress. In that case the
engine should report an indeterminate phase rather than inventing a percentage.
Streaming partials must likewise be emitted only when the underlying runtime
provides a genuine incremental hypothesis.

## Process and artifact ownership

The TUI must be able to cancel the complete child process tree without leaving
containers or model workers behind. Cancellation should be distinguishable
from failure and should preserve completed artifacts when safe.

The engine remains the source of truth for final transcript files, subtitles,
JSON, provenance, and optional event logs. It should write durable outputs
atomically. A TUI crash or terminal disconnect must not corrupt a completed
transcript.

Event ingestion and rendering should be decoupled. Rapid provisional updates
may be coalesced for display, but final, error, cancellation, and completion
events must never be dropped. A useful initial presentation target is to render
an event within roughly 50 milliseconds of receipt; this measures UI overhead,
not ASR inference latency.

## Initial experience

The first TUI should be a viewer and job controller, not a transcript editor.
A conventional Rust implementation could use `ratatui`, `crossterm`, and
`serde_json`.

For long-form jobs, it should show the source file, selected models, current
phase, per-model status, elapsed time, honest progress where available, a
scrolling transcript, warnings, reconstruction state, and final output paths.

For streaming, it should visually separate provisional text from committed
text and show audio position, latency, model/runtime status, and start/stop or
cancel controls. Terminal rendering cannot compensate for a backend that does
not expose incremental hypotheses.

## Phased direction

1. Define and validate the shared event protocol while preserving existing text
   and compact JSON output.
2. Build a read-only TUI for long-form file jobs and the existing streaming
   JSONL adapter.
3. Add ensemble tracks, disagreement/reconstruction status, and richer progress.
4. Add live microphone control after the streaming engine contract is stable.
5. Evaluate editing, detached jobs, reconnection, and other frontends only from
   demonstrated user needs.

Transcript editing, mouse-first interaction, a plugin system, embedded ASR FFI,
GPU controls, web synchronization, and reconnectable background jobs are not
initial requirements.
