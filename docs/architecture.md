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

The default builds target portable modern x86-64 CPUs. They do not use
`-march=native`. A separately named host-native profile can be added only after
portable behavior and benchmark comparability are established.

## Batch and streaming

Recorded, long-form transcription is the primary product path. Offline models
use upstream-supported VAD or explicit chunking rather than a silent monolithic
request. Streaming-capable models retain a stateful file-decoding path before
microphone plumbing is added.

Benchmark records must distinguish raw runtime behavior from production
long-form segmentation. Results using different segmentation strategies are not
presented as direct runtime comparisons without that qualifier.
