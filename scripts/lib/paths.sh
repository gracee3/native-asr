#!/usr/bin/env bash

# Shared host-side storage defaults. Callers may override every path.
native_asr_repo_root() {
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd
}

NATIVE_ASR_REPO_ROOT=${NATIVE_ASR_REPO_ROOT:-$(native_asr_repo_root)}
NATIVE_ASR_MODELS=${NATIVE_ASR_MODELS:-/data/models}
NATIVE_ASR_CACHE=${NATIVE_ASR_CACHE:-/data/cache/native-asr}
NATIVE_ASR_DATASETS=${NATIVE_ASR_DATASETS:-/data/datasets/native-asr}
NATIVE_ASR_BENCHMARKS=${NATIVE_ASR_BENCHMARKS:-/data/benchmarks/native-asr/runs.jsonl}

export NATIVE_ASR_REPO_ROOT NATIVE_ASR_MODELS NATIVE_ASR_CACHE
export NATIVE_ASR_DATASETS NATIVE_ASR_BENCHMARKS
