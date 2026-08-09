set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Build the portable CPU sherpa-onnx runtime image.
build-sherpa:
    docker build --file docker/sherpa-onnx/Dockerfile --tag asr-sherpa-onnx .

# Build the portable CPU NVIDIA NeMo-Speech.cpp runtime image.
build-nemo:
    docker build --file docker/nemo-speech/Dockerfile --tag asr-nemo-speech .

# Build every currently implemented runtime image.
build: build-sherpa build-nemo

# Run host-side syntax, manifest, policy, and wrapper smoke tests.
check:
    @./scripts/check

# Show the pinned model catalog without downloading anything.
list-models:
    @./scripts/models list

# Download every artifact in the current lockfile.
models:
    @./scripts/models fetch --all

# Download all sherpa-onnx model artifacts and their dependencies.
models-sherpa:
    @./scripts/models fetch --runtime sherpa-onnx

# Download all NeMo-Speech.cpp model artifacts.
models-nemo:
    @./scripts/models fetch --runtime nemo-speech

# Download one runtime-qualified model alias.
model alias:
    @./scripts/models fetch {{ quote(alias) }}

# Verify installed models without downloading. An optional alias must exist.
verify-models alias="":
    @./scripts/verify-models {{ if alias == "" { "" } else { quote(alias) } }}

# Transcribe one audio file through the runtime selected by its model alias.
transcribe alias audio:
    @./scripts/transcribe {{ quote(alias) }} {{ quote(audio) }}

# Benchmark one model/audio pair and append a provenance-rich JSONL record.
bench alias audio:
    @./scripts/benchmark {{ quote(alias) }} {{ quote(audio) }}

# Print source versions and revisions pinned by the runtime images.
versions:
    @./scripts/versions

# Print the effective host model directory.
model-dir:
    @./scripts/models path
