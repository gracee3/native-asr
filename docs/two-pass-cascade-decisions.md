# Two-Pass Cascade Discussion and Decision Log

Status: discussion only; no cascade implementation has been authorized or
started on this branch.

Last updated: 2026-08-10

This log records decisions about a possible streaming-first, accurate-second
ASR cascade. It preserves both accepted and rejected choices, their reasons,
and the evidence needed to revisit them. The existing deterministic three-model
ensemble remains the default on `main`; the local-LLM experiments remain
isolated on their experiment branches.

## Current direction

The next implementation candidate is an operational two-pass cascade:

```text
audio
  -> streaming Nemotron: provisional partial text
  -> endpoint or finalized segment
  -> accurate Parakeet: replacement segment text
  -> archival three-model consensus: optional offline mode, not on the live path
```

The first version would not use an LLM. It would preserve explicit provisional,
segment-final, cascade-final, and archival-final states so downstream consumers
can distinguish low-latency text from later replacements.

This is a research starting point, not a final model lock. Google-style two-pass
work motivates the streaming-first/accurate-second topology, but does not prove
that the current Nemotron and Parakeet artifacts are the best pair for this
host or corpus.

## Benchmark-time decision

The remaining benchmark estimate uses the measured real-time factors from the
existing validated subsets and the local LibriSpeech durations. The estimates
are sequential wall-clock time on the current 4-core/8-thread CPU host; parallel
model runs would introduce contention and weaken the comparison.

| Remaining full-split cell | Estimated wall time |
|---|---:|
| `test-clean`, Moonshine small | 1 h 36 min |
| `test-clean`, Whisper small.en | 3 h 19 min |
| `test-other`, Sherpa Parakeet | 44 min |
| `test-other`, NeMo Parakeet TDT | 52 min |
| `test-other`, Moonshine small | 1 h 36 min |
| `test-other`, Whisper small.en | 4 h 24 min |
| Remaining full-split subtotal | about 12 h 31 min |
| Paced and AMI streaming cells | about 40-50 min |

Allow about **13-15 hours** for the targeted remaining matrix, including
validation and ordinary overhead. A clean full-suite pass on a new revision is
more likely **15-17 hours**, because it may repeat completed cells and subset
work. Retries or thermal throttling can make either estimate longer.

Decision: **No, completing every remaining cell is not a prerequisite for
starting the cascade prototype.** The two disjoint 200-utterance snapshots and
existing runtime measurements are sufficient to design the event protocol,
persistent workers, replacement semantics, and failure behavior.

Decision: **Yes, full or appropriately targeted benchmarks are required before
changing the default, locking the model pair, or making accuracy claims.** A
useful near-term gate is the full `test-other` Parakeet comparison, estimated at
about 1 hour 36 minutes of model time and roughly 2 hours with validation. The
remaining matrix can run while the architecture is developed.

Reason: existing ensemble evidence is mixed. Deterministic consensus improved
the original calibration snapshot from the best fixed constituent's 112 errors
to 91, but on the held-out snapshot consensus had 113 errors while the best
single constituent had 106. That is enough evidence to keep measuring, but not
a reason to block protocol work.

## Decisions

| ID | Decision | Status | Reason and revisit condition |
|---|---|---|---|
| C-01 | Preserve the three-model deterministic ensemble as an offline/archival mode. | Yes | It is implemented, auditable, and sometimes improves results. Its mixed held-out result means it should not be assumed superior on every corpus. |
| C-02 | Begin the operational two-pass cascade before the entire benchmark matrix finishes. | Yes | Architecture, state semantics, persistence, and failure handling do not depend on the final model ranking. No implementation is included in this discussion branch. |
| C-03 | Require the entire remaining benchmark matrix before prototype work. | No | It costs roughly 13-15 hours and would not resolve the protocol questions. Require evidence before promotion instead. |
| C-04 | Use Nemotron streaming followed by Parakeet as the first research pairing. | Provisional yes | Both are already pinned and locally exercised, and their roles fit the topology. Revisit after full `test-other`, paced-stream, and cascade-specific measurements. |
| C-05 | Put an LLM adjudicator in cascade version one. | No | The local tie-only experiments did not establish a dependable accuracy/latency benefit. Keep the first cascade deterministic so the topology is the isolated variable. |
| C-06 | Patch upstream to get genuine Nemotron partial results. | No | The pinned NeMo-Speech.cpp C API already supports stream push, result polling, partial/final flags, forced endpoints, and finish. A repository-owned persistent wrapper can expose those features. Sherpa also exposes a stateful online recognizer API. |
| C-07 | Add a local persistent streaming worker/session protocol in a later implementation turn. | Yes, deferred | The current CLI lifecycle does not expose the already available native stream events efficiently. This is local integration work, not an upstream prerequisite. |
| C-08 | Assume the pinned NeMo-Speech.cpp runtime already supplies usable N-best hypotheses. | No | Its public result shape permits alternatives, but the pinned implementation documents one greedy hypothesis today. This blocks standard N-best rescoring, not partial streaming. |
| C-09 | Require upstream work for NeMo-Speech.cpp N-best output. | Likely | True beam N-best hypotheses and scores appear to need upstream implementation or a local upstream-quality patch. Alternatives are a different decoder/runtime or a separate research stack. Confirm before choosing the integration path. |
| C-10 | Add NVIDIA neural rescoring to the first cascade prototype. | No | It first needs a reliable N-best source and tuning data. Evaluate it as a later deterministic accuracy layer, not as a prerequisite for operational two-pass work. |
| C-11 | Evaluate NVIDIA neural rescoring after an N-best oracle/source exists. | Yes, deferred | It scores alternatives rather than rewriting freely and may be cheaper and more reproducible than LLM adjudication. Promotion still requires latency, memory, and held-out accuracy evidence. |
| C-12 | Treat WeNet U2++ as a drop-in postprocessor for current transcripts. | No | U2++ is a separate integrated streaming ASR model family with a CTC first pass and attention-decoder rescoring, plus its own checkpoint and runtime. |
| C-13 | Evaluate a pretrained English WeNet U2++ system as a challenger. | Yes, deferred | It is a proven, coherent two-pass design and can run on x86 CPU. Do not replace current models or commit to training until artifact, license, accuracy, RTF, and RSS are measured locally. |
| C-14 | Train or fine-tune a new model now. | No | First measure pretrained systems and establish the operational cascade. Training would add data, GPU, reproducibility, and maintenance variables prematurely. |
| C-15 | Preserve distinct provisional and final transcript states. | Yes | A streaming cascade must make revisions explicit; silently overwriting partial text would create ambiguous downstream behavior and weak auditability. |

## What NVIDIA neural rescoring is

NVIDIA NeMo neural rescoring is a separate **text language model**, not another
acoustic ASR model and not a generic chat-LLM adjudicator. The ASR decoder first
produces top-K beam hypotheses with scores. The rescorer assigns each text
hypothesis a language-model score, then chooses using a tuned combination of
decoder score, neural-LM score, and sequence length.

The official NeMo tool can use a NeMo Transformer language model or a compatible
autoregressive Hugging Face language model. Its device option supports both CPU
and CUDA, so it is **not GPU-only**. The documented tool is nevertheless a
NeMo/PyTorch research path rather than a ready native persistent-streaming
component.

For this repository it would require:

1. A decoder/runtime that emits real N-best candidates and their scores.
2. A pinned causal language-model artifact and license.
3. A NeMo/PyTorch research environment, or later export to a supported native
   inference representation.
4. Calibration tuning for interpolation and length weights, followed by a
   frozen held-out evaluation.
5. Provenance, timings, memory measurements, and candidate/score audit data.

Hardware decision: **a GPU is optional for inference but helpful for throughput
and strongly preferable for training.** A small pretrained rescorer should be
feasible as a batch experiment on this host's 32 GiB RAM, but the 4-core CPU may
not meet live latency while it also runs ASR. As a planning estimate rather
than a vendor requirement, an 8-12 GiB GPU is a comfortable starting point for
small-model inference; fine-tuning is more realistically a 16-24 GiB-or-larger
task, depending heavily on model size, precision, and batch length. Local RTF
and peak RSS remain the decision criteria.

## What WeNet U2++ is

WeNet U2++ is a separate ASR model and runtime, not merely a language model
attached to Nemotron or Parakeet. Its first pass performs streaming CTC prefix
beam search; its second pass uses attention decoder(s) to rescore those
candidates while reusing the encoder output. A deployable checkpoint brings its
own encoder, CTC head, attention decoder, tokenizer/symbol table, CMVN, and
configuration.

The official WeNet runtime supports x86 CPU through a LibTorch-based C++ path,
so pretrained inference is not GPU-only. Training or substantial fine-tuning is
GPU-oriented. On this host, the right first step would be CPU evaluation of a
pinned pretrained English U2++ checkpoint, with no claim about real-time
performance until RTF and memory are measured. It would be a competing cascade
mode, not an extra stage stacked after the current models.

## Open questions before implementation

- Which native Nemotron path should own the first prototype: the already pinned
  NeMo-Speech.cpp C API, or Sherpa's online recognizer?
- Should accurate second-pass work run per endpointed segment, in a short
  overlapping window, or over a completed recording? This changes correction
  quality and perceived latency.
- What is the replacement contract for timestamps, word confidence, punctuation,
  and downstream consumers when pass two changes pass one text?
- Does the first-pass decoder expose enough uncertainty to avoid running pass two
  on every segment, or should version one always run it for clean attribution?
- Which runtime can provide scored N-best hypotheses without compromising the
  native, reproducible deployment boundary?
- What latency and memory budgets define success on this CPU-only host, and what
  optional GPU profile is worth maintaining?

## Primary references

- Google, [Two-Pass End-to-End Speech Recognition](https://research.google/pubs/two-pass-end-to-end-speech-recognition/)
- Google, [Cascaded encoders for unifying streaming and non-streaming ASR](https://arxiv.org/abs/2010.14606)
- NVIDIA, [NeMo neural rescoring](https://docs.nvidia.com/nemo-framework/user-guide/25.07/nemotoolkit/asr/asr_customization/neural_rescoring.html)
- NVIDIA, [pinned NeMo-Speech.cpp streaming C API](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/5be7bfb104802131e61fe679b3f1401b27270216/include/nemo_speech/asr.h)
- NVIDIA, [pinned NeMo-Speech.cpp live streaming example](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/5be7bfb104802131e61fe679b3f1401b27270216/examples/transcribe_live.cpp)
- Sherpa-ONNX, [online recognizer API](https://k2-fsa.github.io/sherpa/onnx/c-api/html/classsherpa__onnx_1_1cxx_1_1OnlineRecognizer.html)
- Sherpa-ONNX, [two-pass microphone example](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/two-pass-speech-recognition-from-microphone.py)
- WeNet, [runtime overview](https://wenet-e2e.github.io/wenet/runtime.html)
- WeNet, [LibriSpeech tutorial and decoding modes](https://wenet-e2e.github.io/wenet/tutorial_librispeech.html)
- WeNet, [U2++: Unified Two-pass Bidirectional End-to-end Model for Speech Recognition](https://arxiv.org/abs/2106.05642)

