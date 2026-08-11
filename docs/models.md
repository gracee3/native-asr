# Model management

Model weights live under `${NATIVE_ASR_MODELS:-/data/models}` and never in a Docker
image. The committed source of truth is `manifests/models.lock`.

## Lockfile schema

The lockfile is pipe-delimited so Bash can parse it without Python, JSON, or
YAML dependencies. Schema 1 has these fields:

```text
artifact_id
model_alias
runtime
name
source
revision
filename
destination
sha256
license
packaging
requires
notes
```

`source` uses an immutable Hugging Face commit when that is available. Sherpa
model bundles come from its long-lived `asr-models` release; their exact GitHub
asset timestamps and SHA-256 digests are locked because that release tag is not
an immutable content address.

`packaging` is `file`, `tar.bz2`, or `tree`. Multi-file trees have one locked
row per component in `manifests/model-components.lock`, plus an aggregate digest
over the installed per-file inventory. `requires` is a
comma-delimited list of model aliases, used initially for the shared Silero VAD
artifact.

## Download contract

The host downloader:

1. skips an existing artifact only after verification;
2. resumes and retries a `.partial` download;
3. verifies the committed SHA-256 before publication;
4. rejects unsafe archive paths;
5. extracts into a staging directory on the model filesystem;
6. writes a per-file hash inventory for extracted bundles;
7. atomically renames the verified result to its destination;
8. refuses to overwrite an invalid existing destination.

Tree receipts allow `just verify-models` to verify extracted/component files
without network access. Verified compressed archives remain in the external
project cache for reinstall without another download.

## On-disk layout

```text
models/
├── sherpa-onnx/
│   ├── _shared/silero_vad.onnx
│   ├── parakeet-unified-en-0.6b-int8/
│   ├── canary-180m-flash-int8/
│   └── nemotron-streaming-en-560ms-int8/
├── nemo-speech/
    ├── parakeet-tdt-v3/*.gguf
    ├── nemotron-streaming-en/*.gguf
    ├── nemotron-3.5-streaming/*.gguf
│   └── parakeet-ctc-1.1b/*.gguf
├── moonshine/small-streaming-en/*.ort
└── whisper-cpp/
    ├── _shared/ggml-silero-v6.2.0.bin
    └── small.en/ggml-small.en.bin
```

Do not move downloaded files into `docker/` or any other build-context path.

## Interactive model roles

The accepted English interactive path uses exactly two NeMo models from the
read-only tree above:

- `nemo:nemotron-streaming-en` is the persistent first pass. It emits genuine
  provisional revisions, word timings, and 800 ms token-silence phrase finals
  with one 160 ms right-context frame.
- `nemo:parakeet-tdt-v3` is the persistent authoritative correction pass. Each
  finalized phrase is decoded natively through the pinned stable C ABI; an
  empty result receives one bounded silence-only flush retry.

`nemo:nemotron-3.5-streaming` remains a later multilingual option. The
three-model recorded-audio ensemble is independent of this pair and is not in
the interactive critical path. No role changes the storage rule: every GGUF
stays host-managed and is mounted read-only rather than copied into an image.
