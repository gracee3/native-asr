# Roadmap

`native-asr` now has two supported CPU workflows:

1. the deterministic three-model ensemble for recorded audio; and
2. the persistent Nemotron-to-Parakeet cascade for interactive English speech.

Roadmap work must preserve those commands and their provenance, privacy, and
failure semantics. Experimental features should remain optional until they have
repeatable acceptance evidence.

## Highest-value measurements

- Measure the complete three-model ensemble end to end on the current T14,
  including verification, all model passes, alignment, publication, aggregate
  RSS, CPU, and thermals. The current 0.91-1.12 RTF is a historical i7 planning
  estimate from summed constituent runs, not a T14 end-to-end result.
- Extend public long-form validation beyond the deterministic 100-utterance
  snapshots without silently mixing segmentation policies or private audio.
- Repeat interactive acceptance after runtime, model, endpointing, or host
  changes; keep paced latency evidence separate from unpaced throughput.

## Long-form candidates

- Keep model workers persistent across long jobs where runtime APIs permit it.
- Measure controlled two-worker scheduling only after a clean sequential T14
  baseline. CPU, cache, memory bandwidth, and thermals are shared, so parallel
  inference is not assumed to be faster.
- Consider optional local-LLM adjudication only for bounded three-way
  disagreement regions. Agreed text and the deterministic consensus must remain
  available and unchanged by default.
- Add richer progress events without inventing percentages for runtimes that do
  not expose them.

## Interactive candidates

- Evaluate the multilingual Nemotron 3.5 model as a separate profile rather
  than changing the accepted English defaults.
- Build richer clients, including a possible Rust TUI, on the canonical JSONL
  event protocol. Inference and durable output remain headless engine concerns.
- Add live controls only as explicit user actions; automated tests must continue
  to use paced public audio and never activate a microphone.

## Outside the current guarantees

GPU execution, diarization, transcript editing, web synchronization,
reconnectable background services, and unrestricted multi-model concurrency are
not part of the current supported milestone.
