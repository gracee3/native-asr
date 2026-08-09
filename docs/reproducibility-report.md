# Reproducibility report: 2026-08-09

## Validated state

Large artifacts use the centralized defaults: models at `/data/models`, cache at
`/data/cache/native-asr`, datasets at `/data/datasets/native-asr`, and benchmark
summaries at `/data/benchmarks/native-asr/runs.jsonl`. The checkout compatibility
path `models` resolves to `/data/models`. Docker's global root remains
`/var/lib/docker`.

All nine ASR aliases and both runtime VAD dependencies passed locked SHA-256
verification. Prepared dataset manifests contain 2,620 `test-clean` utterances,
2,939 `test-other` utterances, and the AMI ES2004a meeting. The deterministic
five-minute `test-other` derivative contains 54 complete utterances separated by
250 ms silence, is exactly 300 seconds, and has SHA-256
`79e09f0e3d642665702a8b8880cd58cb98f8a6248eaef5185eb7cb168fff78eb`.
No utterance is truncated while retaining its full reference.

All four images rebuilt successfully and passed the non-root, Python-free,
model-free, and network-disabled inference gates:

| Image | Image ID | Bytes | Configured user |
|---|---|---:|---|
| `asr-sherpa-onnx` | `sha256:8d9cb1be58249f65dcb8b6bf0c78c56d72502d8b58e6d1129e4c61053a0cde8f` | 208,228,799 | `65532:65532` |
| `asr-nemo-speech` | `sha256:fe6080c29698d70cfd2b79ffc34abb56913683f3b310cf9d09edda12cb94eb5f` | 200,587,544 | `65532:65532` |
| `asr-moonshine` | `sha256:846ceb02b61f6fed0feb7e38f7c7574a973f9a2f27b8b50839d778d4d70b2302` | 214,393,412 | `65532:65532` |
| `asr-whisper-cpp` | `sha256:5c8e4e65f52fec4f49e758ec7998de2031006cea2567d49f6e07f5d5e9e11d54` | 199,119,113 | `65532:65532` |

## Nine-alias engineering validation

The table below is a dispatch, model-reuse, scoring, and metrics validation on
the same SHA-256-ranked first two `test-clean` utterances (7.59 seconds and 44
reference words). It is deliberately too small to be an accuracy ranking. Every
run loaded its model once, produced two independent hypotheses, and had zero
failures. Each batch process loaded its model once and reused it for both
utterances; a separate one-utterance cold calibration probe records startup,
model load, and first-inference wall time outside the batch measurement.

| Model | Run ID | WER | RTF | Peak RSS KiB |
|---|---|---:|---:|---:|
| `sherpa:canary-180m-flash` | `20260809T023808598980Z-63b9c4f275ed` | 4.55% | 0.265 | 395,356 |
| `nemo:parakeet-tdt-v3` | `20260809T023820998253Z-fc5593ce457b` | 9.09% | 0.255 | 754,592 |
| `nemo:nemotron-streaming-en` | `20260809T023824976400Z-25e2e7ea866c` | 9.09% | 0.571 | 961,972 |
| `nemo:nemotron-3.5-streaming` | `20260809T023832802876Z-657b9c5f56b3` | 9.09% | 0.579 | 960,564 |
| `whisper:small.en` | `20260809T023906750962Z-51c1757ec62f` | 9.09% | 0.997 | 771,704 |
| `sherpa:parakeet-unified-en` | `20260809T023803501599Z-5ff1b6eb7402` | 13.64% | 0.331 | 1,027,432 |
| `sherpa:nemotron-streaming-en` | `20260809T023812596102Z-a408443e25d0` | 13.64% | 0.574 | 1,015,252 |
| `nemo:parakeet-ctc-1.1b` | `20260809T023841046569Z-59a17eabe0ca` | 13.64% | 1.468 | 1,230,120 |
| `moonshine:small-streaming-en` | `20260809T023902542412Z-4ea4a1356f26` | 13.64% | 0.340 | 600,684 |

These runs record Git revision `c777eb154a1d6d13601cacd38b817db61c295e00`
with `dirty_tree=true` because the third-commit benchmark implementation was
under final validation. The image, model, dataset, preprocessing, options, and
exact host-adapter digest are all part of each resume fingerprint; the tested
adapter content is the content committed with this report.

## Streaming validation

All four stateful aliases completed the 13.69-second shared sample with ordered,
contiguous event sequences and measured in-container CPU/RSS metrics:

| Model | Events | Partials | RTF | Peak RSS KiB |
|---|---:|---:|---:|---:|
| `sherpa:nemotron-streaming-en` | 2 | 0 | 0.488 | 978,128 |
| `nemo:nemotron-streaming-en` | 2 | 0 | 0.459 | 962,036 |
| `nemo:nemotron-3.5-streaming` | 2 | 0 | 0.458 | 960,476 |
| `moonshine:small-streaming-en` | 30 | 27 | 0.395 | 640,420 |

Moonshine also completed a persisted one-utterance stream validation as run
`20260809T023717610933Z-4ad1c4c2a772`: 21 events, 19 native partials, 3.57% WER,
and 688,036 KiB peak RSS. Resume was then verified to reuse that successful
fingerprint without creating another detail file.

The pinned Sherpa and NeMo command-line tools perform stateful streaming-file
decode but do not expose incremental hypothesis callbacks. Their adapters
therefore emit a truthful final plus metrics pair and explicitly report zero
partials. Moonshine is replayed in real 20 ms stateful chunks and exposes native
partial revisions.

## Staged-suite status and limitations

`just bench-suite staged` now implements the complete deterministic matrix:
both 100-utterance LibriSpeech subsets across nine aliases, full splits across
four finalists, the five-minute paced stream, and unpaced full AMI streaming.
It is resumable only on an exact runtime image, model, dataset, preprocessing,
adapter, and options fingerprint, and it never reports an overlap-sensitive AMI
full-meeting WER.

The full staged matrix was not executed during this implementation pass; it is
many CPU-hours beyond the engineering validation above. Consequently this
report publishes no purported full-split rankings. The committed suite and the
verified external artifacts are ready for a resumable dedicated run.

The official Moonshine v0.1.1 Small Streaming component catalog currently
installs 247,255,694 bytes across eight locked files, rather than the earlier
approximate 165 MB planning figure. The lock records the observed file sizes and
digests, and that verified upstream tree is what these results used.
