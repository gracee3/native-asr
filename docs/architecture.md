# Architecture

`native-asr` keeps three independent layers:

```text
runtime image
    +
host model weights mounted at /models:ro
    +
host audio mounted read-only
```

## Non-negotiable boundaries

- Docker build contexts exclude model and audio extensions.
- Dockerfiles contain no model URL, `COPY models`, or runtime download step.
- Model downloads happen only through host-side scripts.
- Inference uses explicit local paths and Docker networking is disabled.
- Rebuilding or deleting an image does not modify the model directory.
- Original recordings are read-only. Normalization uses temporary files.
- Final runtime images contain no Python, pip, PyTorch, or NVIDIA NeMo.

The stable container model mount is `/models`. Host tooling may mount an audio
file's parent directory at `/audio` or a working directory at `/work`, but the
source mount remains read-only.

Host defaults are centralized in `scripts/lib/paths.sh`: `/data/models`,
`/data/cache/native-asr`, `/data/datasets/native-asr`, and
`/data/benchmarks/native-asr/runs.jsonl`. Matching environment variables are
the override mechanism. Docker's global data root is intentionally unchanged.

## Runtime images

| Image | Native engine | Priority |
|---|---|---:|
| `asr-sherpa-onnx` | sherpa-onnx + pinned ONNX Runtime | 1 |
| `asr-nemo-speech` | NVIDIA NeMo-Speech.cpp + ggml | 2 |
| `asr-moonshine` | Moonshine native C++ runtime | 3 |
| `asr-whisper-cpp` | ggml-org/whisper.cpp | 3 |

The Sherpa image links its selected native executables to the pinned shared
ONNX Runtime library. This avoids a verified allocator failure seen with the
same release's static ONNX Runtime archive while keeping compilers, headers,
and source trees out of the final stage.

The NeMo-Speech.cpp image builds the pinned ASR CLI and shared library against
its exact ggml submodule. CUDA, Metal, Vulkan, HTTP, gRPC, NMT, TTS,
diarization, examples, tests, and tools are disabled. The final stage contains
the staged native libraries, SentencePiece, FFmpeg, and jq, but no compiler,
source checkout, Python, PyTorch, or NVIDIA NeMo framework.

NeMo-Speech.cpp 1.0.0 currently passes a literal four threads to ggml ASR graph
execution. The host and container wrappers enforce and record four until the
upstream runtime exposes a real ASR thread control.

The Moonshine image links a repository-owned C++ adapter to the official
v0.1.1 Linux library release, whose SHA-256 and source revision are pinned. The
adapter keeps one transcriber alive across batch streams and emits native
partial/final events. The whisper.cpp image builds `whisper-cli` from v1.9.2
with GPU backends and host-native code generation disabled.

The default builds target portable modern x86-64 CPUs. They do not use
`-march=native`. A separately named host-native profile can be added only after
portable behavior and benchmark comparability are established.

## Long-form and streaming

The repository exposes two supported products over the same runtime boundary.
The long-form path runs three complete model tracks sequentially and produces a
deterministic consensus audit. Offline models use upstream-supported VAD or
explicit chunking rather than a silent monolithic request. The interactive
path is the T14-local English Nemotron-to-Parakeet cascade specified in
`docs/interactive-cascade.md`.

The cascade adds a native supervisor and stable-C-ABI worker to the existing
NeMo image. Host PipeWire capture or file decoding writes 16 kHz mono float PCM
to container stdin; no sound device enters the container. Nemotron stays
resident and produces provisional text and endpoints while a separate,
restartable resident Parakeet worker corrects finalized phrases. The recorded-
audio ensemble remains independent of this path.

The Rust `native-asr-tui` binary is a live-only client, not an inference
runtime. It enumerates and revalidates an explicit PipeWire source, launches the
headless wrapper in a separate Unix process group, consumes canonical JSONL on
threads and channels, and keeps committed, correction-pending, and provisional
text in separate UI state. File replay stays on `scripts/cascade file`.

Benchmark records must distinguish raw runtime behavior from production
long-form segmentation. Results using different segmentation strategies are not
presented as direct runtime comparisons without that qualifier.

## Host dispatcher

`scripts/transcribe` resolves a runtime-qualified alias from the model lock,
verifies it, and invokes the matching image with `--network none`. Only the
runtime's model subtree and the input file's parent directory are mounted, both
read-only. Container wrappers create normalized audio under temporary storage
and emit JSON; the host enriches that JSON with the original absolute audio path
and immutable artifact provenance.

`scripts/ensemble` composes that boundary without bypassing it. It verifies all
three configured models before starting, then invokes one complete measured
`scripts/transcribe --format json` process group per model, sequentially. The
host-only standard-library consensus layer preserves the three native results,
aligns normalized lexical tokens to the first track, and publishes the audit
directory atomically. It does not place Python or model data in a runtime image.
