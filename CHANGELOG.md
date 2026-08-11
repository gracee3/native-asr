# Changelog

All notable user-facing changes are recorded here.

## Unreleased

- Added the live-only `native-asr-tui` Rust client with deliberate PipeWire
  source selection, provisional/correction/commit views, latency and
  degradation status, optional audit destination entry, and terminal-safe
  graceful/hard shutdown.
- Added `scripts/cascade live --control-stdin`: EOF stops safely before
  readiness or flushes an active session, while `INT`/`TERM` remain hard
  cancellation with status 130 and no successful audit.
- Added locked Rust formatting, Clippy, unit/process/UI/pseudo-terminal tests,
  release build recipes, streaming doctor checks, and host-only CI coverage.

## v0.1.0 - 2026-08-11

The first release supports two complete offline, CPU-only workflows on x86-64
Linux:

- a deterministic NeMo, Sherpa, and whisper.cpp ensemble for auditable
  long-form transcription; and
- a persistent Nemotron-to-Parakeet cascade for provisional and committed
  English streaming text.

### Usability

- Added `just setup-long-form` and `just setup-streaming` to build the required
  native images and fetch the locked models for each workflow.
- Added `scripts/doctor [all|long-form|streaming]` and `just doctor PROFILE` for
  actionable readiness checks.
- Made `scripts/cascade --help` available at the top level and delayed live
  capture until both model workers emit the stable `cascade: ready` signal.
- Added deterministic file and isolated PipeWire-loopback benchmark transports.

### T14 release acceptance

Both 100-utterance PipeWire-loopback fixtures passed all 13 gates from a clean
`8f6ef4e` revision using image
`sha256:a170c7810eb41f5d5f3ec5fb7022709aa3bf6276a04964473c98a76665217250`.

| Fixture | Nemotron WER | Committed WER | Partial p95 | Correction max | Degraded | RTF |
|---|---:|---:|---:|---:|---:|---:|
| LibriSpeech `test-clean`, 100 utterances | 4.87% | 4.35% | 142 ms | 847 ms | 0 | 1.00076 |
| LibriSpeech `test-other`, 100 utterances | 7.65% | 6.17% | 138 ms | 1,002 ms | 0 | 1.00081 |

Each run loaded each model once, showed no swap growth, preserved contiguous
events and ordered commits, linked only the uniquely named virtual nodes, and
completely removed its playback, capture, and virtual-node resources. Raw
events, audits, and generated public-corpus audio remain external to Git.

### Known limitations

- The supported deployment target is CPU-only x86-64 Linux; live capture
  requires a working PipeWire user session.
- Streaming recognition is English-only. A physical-microphone smoke test is
  optional and was not part of automated release acceptance.
- GPU execution, diarization, LLM adjudication, and transcript editing are not
  included in `v0.1.0`. The Rust TUI is an unreleased post-tag addition.
- The reported WERs use deterministic 100-utterance public-corpus fixtures and
  are engineering baselines, not universal accuracy claims.
- The T14 package sensor peaked at 100 C and 98 C; temperature was recorded but
  was not an acceptance threshold.
