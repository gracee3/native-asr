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

The repository foundation and reproducible model lockfile are in place. The
runtime images and common transcription interface are being implemented in this
order:

1. sherpa-onnx;
2. NeMo-Speech.cpp;
3. common transcription and benchmark harness;
4. Moonshine and whisper.cpp.

## Architecture

```text
native runtime image       host model directory       host audio
(no model weights)    +    (read-only /models)   +    (read-only input)
```

The model directory defaults to `./models` and can be placed elsewhere:

```bash
export NATIVE_ASR_MODELS=/data/native-asr-models
```

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

Downloads resume when possible, land in a temporary file, are verified against
the committed SHA-256, and are atomically published. Existing valid files are
not downloaded again. `HF_TOKEN` is supported for a future gated Hugging Face
artifact, but none of the initial artifacts require it.

Verify installed artifacts without network access:

```bash
just verify-models
just verify-models sherpa:parakeet-unified-en
```

The first form verifies everything currently installed and reports models that
have not been installed. The second requires and verifies the named model.

## Intended transcription interface

The common interface will be:

```bash
just build-sherpa
just model sherpa:parakeet-unified-en
just transcribe sherpa:parakeet-unified-en recording.m4a
```

Its equivalent Docker boundary is deliberately explicit:

```bash
docker run --rm --network none \
  -v "$NATIVE_ASR_MODELS/sherpa-onnx:/models:ro" \
  -v "$PWD:/work:ro" \
  asr-sherpa-onnx \
  transcribe --model /models/parakeet-unified-en-0.6b-int8 /work/recording.wav
```

Audio normalization uses a temporary 16 kHz mono PCM16 WAV and never modifies
the original recording.

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

Every URL, upstream revision, digest, license identifier, quantization, and
packaging rule is recorded in [`manifests/models.lock`](manifests/models.lock).

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layer boundaries and image invariants
- [`docs/models.md`](docs/models.md) — lockfile and on-disk model contract
- [`docs/benchmarking.md`](docs/benchmarking.md) — benchmark and accuracy metadata contract

Private recordings, transcripts, downloaded weights, and benchmark outputs are
ignored by both Git and the Docker build context.
