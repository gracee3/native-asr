set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Build the portable CPU sherpa-onnx runtime image.
build-sherpa:
    docker build --file docker/sherpa-onnx/Dockerfile --tag asr-sherpa-onnx .

# Build the portable CPU NVIDIA NeMo-Speech.cpp runtime image.
build-nemo:
    docker build --file docker/nemo-speech/Dockerfile --tag asr-nemo-speech .

# Build the pinned Moonshine native streaming runtime image.
build-moonshine:
    docker build --file docker/moonshine/Dockerfile --tag asr-moonshine .

# Build the pinned whisper.cpp CPU runtime image.
build-whisper:
    docker build --file docker/whisper-cpp/Dockerfile --tag asr-whisper-cpp .

# Build the pinned llama.cpp CPU adjudicator runtime image.
build-llama:
    docker build --file docker/llama-cpp/Dockerfile --tag asr-llama-cpp .

# Build every currently implemented runtime image.
build: build-sherpa build-nemo build-moonshine build-whisper build-llama

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

models-moonshine:
    @./scripts/models fetch --runtime moonshine

models-whisper:
    @./scripts/models fetch --runtime whisper-cpp

models-llm:
    @./scripts/models fetch --runtime llama-cpp

# Download one runtime-qualified model alias.
model alias:
    @./scripts/models fetch {{ quote(alias) }}

# Verify installed models without downloading. An optional alias must exist.
verify-models alias="":
    @./scripts/verify-models {{ if alias == "" { "" } else { quote(alias) } }}

# Transcribe one audio file through the runtime selected by its model alias.
transcribe alias audio:
    @./scripts/transcribe {{ quote(alias) }} {{ quote(audio) }}

# Run the three-model long-form ensemble; the optional LLM remains explicitly opt-in.
ensemble audio output adjudicator="":
    @./scripts/ensemble --output {{ quote(output) }} {{ if adjudicator == "" { "" } else { "--adjudicator " + quote(adjudicator) } }} {{ quote(audio) }}

# Recreate the locked 123.95-second public long-form fixture.
ensemble-fixture output:
    @./scripts/recreate-long-form-fixture {{ quote(output) }}

# Run both local-LLM candidates twice over calibration and held-out snapshots.
adjudication-benchmark output:
    @./scripts/adjudication-benchmark --output {{ quote(output) }}

# Benchmark one model/audio pair and append a provenance-rich JSONL record.
bench alias audio:
    @./scripts/benchmark {{ quote(alias) }} {{ quote(audio) }}

# Locked public evaluation corpora.
datasets:
    @./scripts/datasets fetch

dataset dataset:
    @./scripts/datasets fetch {{ quote(dataset) }}

prepare-datasets:
    @./scripts/datasets prepare

verify-datasets:
    @./scripts/datasets verify

dataset-dir:
    @./scripts/datasets path

benchmark-set alias dataset:
    @./scripts/benchmark-set {{ quote(alias) }} {{ quote(dataset) }}

bench-suite suite:
    @./scripts/benchmark-suite {{ quote(suite) }}

# Run the reviewed sequential benchmark matrix with locking and validation.
bench-full:
    @./scripts/run-full-benchmark

# Run the two full test-clean finalist cells used for targeted recovery.
bench-test-clean-pair:
    @./scripts/run-test-clean-pair

stream alias audio:
    @./scripts/stream {{ quote(alias) }} {{ quote(audio) }}

# Print source versions and revisions pinned by the runtime images.
versions:
    @./scripts/versions

# Print the effective host model directory.
model-dir:
    @./scripts/models path
