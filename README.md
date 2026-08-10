# native-asr

`native-asr` is a CPU-first, fully offline speech-recognition toolkit for
recorded audio and streaming experiments. It packages lightweight native
inference runtimes separately from model weights and user audio.

The central rule is simple:

> Runtime images are redistributable tooling; model weights stay external on
> the host.

No Dockerfile may copy, download, or embed a model. Model acquisition is an
explicit host-side operation, model directories are mounted read-only at
`/models`, and inference containers run with networking disabled.

## Status

Four pinned native CPU runtimes, nine ASR aliases, locked public evaluation
corpora, stateful streaming adapters, and reproducible WER/performance suites
share one model-free, network-disabled container interface.

The deterministic 100-utterance gate is complete for all nine aliases on both
LibriSpeech splits: 18/18 runs completed with zero failures. A lower-commitment
full-corpus checkpoint is also complete for Sherpa Parakeet and NeMo Parakeet
TDT v3 on all 2,620 `test-clean` utterances. Those are two of the intended
eight full-split finalist cells; the other six and the final streaming matrix
remain pending.

## Validated benchmark snapshot

These are the SHA-256-ranked 100-utterance gate results from 2026-08-09, not a
claimed full-split ranking. Every cell used the same clean Git revision
`797eb65`, locked artifacts, `english-upper-apostrophe-v1` WER normalization,
and CPU-only containers on an Intel Core i7-1185G7 (4 cores / 8 threads,
31.1 GiB RAM).

| Model | `test-clean` WER | `test-other` WER | `test-clean` RTF | `test-other` RTF | Max RSS GiB |
|---|---:|---:|---:|---:|---:|
| `sherpa:parakeet-unified-en` | 4.46% | 3.59% | 0.131 | 0.136 | 3.50 |
| `sherpa:canary-180m-flash` | 7.25% | 4.07% | 0.101 | 0.108 | 0.70 |
| `sherpa:nemotron-streaming-en` | 6.38% | 9.73% | 0.112 | 0.116 | 6.85 |
| `nemo:parakeet-tdt-v3` | 2.02% | 4.26% | 0.162 | 0.162 | 0.91 |
| `nemo:nemotron-streaming-en` | 2.59% | 6.26% | 0.297 | 0.322 | 0.92 |
| `nemo:nemotron-3.5-streaming` | 4.17% | 9.67% | 0.334 | 0.320 | 0.92 |
| `nemo:parakeet-ctc-1.1b` | 2.45% | 4.13% | 0.280 | 0.276 | 1.50 |
| `moonshine:small-streaming-en` | 6.33% | 14.10% | 0.297 | 0.298 | 0.67 |
| `whisper:small.en` | 2.88% | 7.29% | 0.615 | 0.824 | 0.74 |

Lower is better for both WER and RTF; RTF below `1.0` is faster than real time.
RTF is batch wall time divided by audio duration and excludes the separately
recorded cold-start probe. Max RSS is the larger in-container peak from the two
rows. Runtime-specific, fingerprinted batching policies are part of every run,
so this small gate is best read as failure/regression coverage. Exact run IDs,
image IDs, policies, and limitations are in the
[`reproducibility report`](docs/reproducibility-report.md).
The exact 18 summary records and their 1,800 per-utterance detail records are
published in the
[`2026-08-09 benchmark snapshot`](benchmarks/published/2026-08-09-librispeech-100/README.md).

## Initial validated full-split checkpoint

These are the first two complete `test-clean` finalist cells, not a ranking of
all four finalists. Both used the same 2,620-utterance, 5.403-hour manifest,
clean Git revision `44f2eaf`, locked artifacts, CPU-only containers, and the
same normalization and host described above. Every utterance detail row
validated with zero runtime failures.

| Model | WER | S / D / I | RTF | Peak RSS GiB | Model loads | Run ID |
|---|---:|---:|---:|---:|---:|---|
| `sherpa:parakeet-unified-en` | 2.12% | 809 / 206 / 101 | 0.141 | 3.66 | 378 | `20260809T210123493196Z-ab7728fffe58` |
| `nemo:parakeet-tdt-v3` | 2.15% | 915 / 97 / 119 | 0.160 | 0.99 | 1 | `20260809T214700876989Z-7fed3a8f2e1c` |

The Sherpa adapter's fingerprinted v4 policy includes every retry in timing and
memory accounting; it recursively isolates exit-zero empty groups and uses
lossless balanced chunks only when the pinned VAD also returns empty. The exact
two summaries and 5,240 public-corpus detail records are in the
[`full test-clean checkpoint`](benchmarks/published/2026-08-09-librispeech-test-clean-pair/README.md).
Interpretation, the rejected pre-fix attempt, and remaining work are documented
in the [`reproducibility report`](docs/reproducibility-report.md).

## Architecture

```text
native runtime image       host model directory       host audio
(no model weights)    +    (read-only /models)   +    (read-only input)
```

Large artifacts default to the dedicated `/data` filesystem:

```bash
NATIVE_ASR_MODELS=/data/models
NATIVE_ASR_CACHE=/data/cache/native-asr
NATIVE_ASR_DATASETS=/data/datasets/native-asr
NATIVE_ASR_BENCHMARKS=/data/benchmarks/native-asr/runs.jsonl
```

Each variable remains an explicit environment override. A checkout-local
`models` symlink is supported for compatibility.

The directory is never used as a Docker build input. Deleting or rebuilding an
image therefore does not remove a model or require it to be downloaded again.

## Prerequisites

- Bash, `curl`, `sha256sum`, and standard archive tools for model management;
- [Just](https://github.com/casey/just) for the primary command interface;
- Docker for building and running the inference images;
- an x86-64 Linux CPU for the initial images.

Python, PyTorch, NVIDIA NeMo, and Hugging Face Python libraries are not part of
the deployed inference path.

## Model management

List the pinned artifacts without downloading them:

```bash
just list-models
```

Download one model, all models for a runtime, or every currently locked model:

```bash
just model sherpa:parakeet-unified-en
just models-sherpa
just models-nemo
just models
```

Downloads resume in the project cache, are verified against committed SHA-256
digests, and are installed through same-filesystem staging and atomic rename.
Verified archives remain cached. Existing valid files are not downloaded again;
invalid destinations are never overwritten. `HF_TOKEN` is supported, but none
of the locked artifacts require it.

Verify installed artifacts without network access:

```bash
just verify-models
just verify-models sherpa:parakeet-unified-en
```

The first form verifies everything currently installed and reports models that
have not been installed. The second requires and verifies the named model.

## Sherpa transcription

Build the pinned sherpa-onnx 1.13.2 / ONNX Runtime 1.24.4 CPU image and fetch a
model:

```bash
just build-sherpa
just model sherpa:canary-180m-flash
```

Run inference with no container network and read-only model/audio mounts:

```bash
docker run --rm --network none \
  -v "${NATIVE_ASR_MODELS:-/data/models}/sherpa-onnx:/models:ro" \
  -v "$PWD:/work:ro" \
  asr-sherpa-onnx \
  transcribe \
  --model /models/canary-180m-flash-int8 \
  --format json \
  /work/recording.m4a
```

The image normalizes common media formats with FFmpeg, defaults offline models
to the shared Silero VAD when installed, and emits text or compact JSON. The
Nemotron model uses Sherpa's true stateful streaming decoder for file input.

## NeMo-Speech.cpp transcription

Build the pinned NeMo-Speech.cpp 1.0.0 CPU image and fetch an official NVIDIA
Q8 GGUF:

```bash
just build-nemo
just model nemo:parakeet-tdt-v3
```

The image builds only the native CLI and ASR library with its pinned ggml
submodule. Parakeet TDT v3 and CTC run in full-utterance mode; the two Nemotron
models use the runtime's cache-aware streaming-file path by default.

The pinned upstream ASR runtime currently fixes ggml compute at four CPU
threads. The wrapper reports and enforces that value so benchmark metadata does
not imply a thread count the engine did not use.

## Moonshine and whisper.cpp

Moonshine v0.1.1 uses its native C++ streaming interface and the locked Small
Streaming English ORT component tree. whisper.cpp v1.9.2 is built CPU-only and
uses the official F16 `small.en` model plus Silero VAD v6.2.0:

```bash
just build-moonshine
just models-moonshine
just transcribe moonshine:small-streaming-en recording.m4a

just build-whisper
just models-whisper
just transcribe whisper:small.en recording.m4a
```

## Common interface

The runtime-independent path is:

```bash
just build-sherpa
just model sherpa:parakeet-unified-en
just transcribe sherpa:parakeet-unified-en recording.m4a
```

It verifies the selected artifact, chooses the image and model path from the
lockfile, mounts models and audio read-only, and always passes `--network none`.
Containers use the invoking non-root UID/GID so private readable audio does not
need to be made world-readable; a root caller falls back to the image's
unprivileged `65532:65532` identity.
Structured output retains the original absolute host audio path and locked
artifact provenance:

```bash
./scripts/transcribe --format json --threads 8 \
  sherpa:parakeet-unified-en recording.m4a
```

Audio normalization uses a temporary 16 kHz mono PCM16 WAV and never modifies
the original recording. `--language`, `--output`, `--vad`, and `--stream` are
available through the script without expanding the simple Just recipe.

## Deterministic three-model ensemble

Run the default English CPU ensemble sequentially and publish a private,
provenance-complete audit directory:

```bash
just ensemble recording.m4a recording.audit
```

The command verifies all models before inference, uses NeMo Parakeet TDT as the
default alignment anchor, applies deterministic 2-of-3 token/deletion
consensus, prints the final text, and never overwrites the explicit output
path. Successful bundles contain `transcript.txt`, exact runtime tracks,
alignment and disagreement records, stderr logs, timing, image IDs, artifact
digests, and adapter/Git fingerprints. Failed or cancelled started jobs retain
their evidence but do not publish an authoritative transcript. See
[`docs/ensemble.md`](docs/ensemble.md) for ordering overrides, exit statuses,
alignment rules, timing semantics, and the complete artifact contract.

## Benchmarking

Append a provenance-rich JSONL result and print a concise summary:

```bash
just bench sherpa:parakeet-unified-en recording.m4a
```

The timed region includes container startup, normalization, model loading, and
inference. It excludes prior model checksum verification and image inspection.
Single-file records and set/suite summaries default to the external benchmark
store. Dataset runs retain raw and normalized text, use versioned English WER,
apply a fingerprinted runtime-specific batching policy, and resume only when
every fingerprint matches:

```bash
just datasets
just prepare-datasets
just benchmark-set whisper:small.en librispeech-test-clean
just bench-suite staged
```

After `just prepare-datasets`, run the complete reviewed matrix with
clean-tree, stable-power, locking, phase, and result validation gates:

```bash
just bench-full
```

The runner honors all `NATIVE_ASR_*` storage overrides and automatically uses
`systemd-inhibit`, when available, to block sleep for the duration. Set
`NATIVE_ASR_INHIBIT_SLEEP=0` to opt out or `NATIVE_ASR_SKIP_POWER_CHECK=1` when
AC/performance-profile detection is unavailable. `just bench-test-clean-pair`
replays only the two full `test-clean` finalist cells used by the original
targeted recovery run.

The staged suite evaluates deterministic 100-utterance subsets, the four fixed
finalists on full LibriSpeech splits, a reproducible five-minute paced stream,
and the full AMI meeting. AMI is not assigned a misleading overlap-sensitive
full-meeting WER.

Dataset evaluation uses a fingerprinted batching policy for each runtime.
Most runtimes reuse one model load across the set; Sherpa Parakeet uses bounded
groups and routes utterances over 20 seconds through the same pinned Silero VAD
path used for production long-form transcription. If the upstream CLI exits
zero with an empty group, the adapter recursively isolates the affected input,
then tries VAD and finally balanced lossless chunks of at most 10 seconds. All
fallback processes contribute to wall time, CPU time, peak RSS, and model-load
counts; nonzero runtime exits are never upgraded to success.

Show the runtime source pins at any time:

```bash
just versions
```

## Initial model set

| Alias | Role | Runtime form |
|---|---|---|
| `sherpa:parakeet-unified-en` | primary English long-form model | INT8 ONNX, offline with VAD |
| `sherpa:canary-180m-flash` | small multilingual challenger | INT8 ONNX, offline |
| `sherpa:nemotron-streaming-en` | streaming and cross-runtime comparison | 560 ms INT8 ONNX |
| `nemo:parakeet-tdt-v3` | primary NeMo-Speech.cpp long-form model | Q8 GGUF, offline |
| `nemo:nemotron-streaming-en` | streaming and cross-runtime comparison | Q8 GGUF |
| `nemo:nemotron-3.5-streaming` | multilingual streaming phase two | Q8 GGUF |
| `nemo:parakeet-ctc-1.1b` | experimental batch-throughput model | Q8 GGUF |
| `moonshine:small-streaming-en` | low-latency stateful English model | quantized multi-file ORT tree |
| `whisper:small.en` | established English accuracy baseline | F16 GGML with Silero VAD |

Every URL, upstream revision, digest, license identifier, quantization, and
packaging rule is recorded in [`manifests/models.lock`](manifests/models.lock).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layer boundaries and image invariants
- [`docs/models.md`](docs/models.md) — lockfile and on-disk model contract
- [`docs/benchmarking.md`](docs/benchmarking.md) — benchmark and accuracy metadata contract
- [`docs/ensemble.md`](docs/ensemble.md) — deterministic consensus command and audit contract
- [`docs/two-pass-cascade-decisions.md`](docs/two-pass-cascade-decisions.md) — two-pass cascade discussion, decisions, and research gates
- [`docs/reproducibility-report.md`](docs/reproducibility-report.md) — validated images, run IDs, and current limitations
- [`docs/future-ensemble-reconstruction.md`](docs/future-ensemble-reconstruction.md) — deferred LLM, concurrency, and streaming direction
- [`docs/future-rust-tui.md`](docs/future-rust-tui.md) — deferred terminal UI and event-protocol direction

Private recordings, transcripts, downloaded weights, and local benchmark
outputs are ignored by Git and the Docker build context. Curated benchmark
snapshots derived only from public evaluation corpora may be published under
`benchmarks/published`; benchmark data remains excluded from runtime images.

## License

The original code, tests, and documentation in this repository are licensed
under the [MIT License](LICENSE). This grant does not relicense upstream native
runtimes, external model weights, or evaluation datasets. Their licenses and
exact source revisions are recorded in the lockfiles and retained in built
images where applicable.
