set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

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

# Print the effective host model directory.
model-dir:
    @./scripts/models path
