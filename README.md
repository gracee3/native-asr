# native-asr

Offline, CPU-only speech recognition for long recordings and live English
transcription. `native-asr` provides two complete workflows on ordinary x86-64
Linux hardware:

- a deterministic three-model ensemble for accurate, auditable long-form
  transcripts; and
- a low-latency Nemotron-to-Parakeet cascade for provisional and committed
  streaming text.

Inference is local. Containers run without network access, model weights remain
on the host, and source recordings are mounted read-only.

## Choose a workflow

| | Long-form ensemble | Interactive cascade |
|---|---|---|
| Best for | Interviews, meetings, lectures, archival recordings | Live captions, dictation, interactive applications |
| Models | NeMo Parakeet + Sherpa Parakeet + whisper.cpp | Nemotron streaming + NeMo Parakeet |
| Result | One deterministic 2-of-3 consensus transcript | Provisional text followed by authoritative phrase commits |
| Execution | Three sequential offline passes | Two persistent native workers |
| Output | Private, provenance-complete audit bundle | Terminal text or canonical JSONL; audit is optional |

### Approximate accuracy and speed

Lower is better for both word error rate (WER) and real-time factor (RTF). An
RTF of `1.0` keeps pace with the audio; `0.5` processes it at about twice real
time.

| Workflow | LibriSpeech `test-clean` WER | LibriSpeech `test-other` WER | Approximate RTF |
|---|---:|---:|---:|
| Long-form three-model ensemble | 1.78% | 3.28% | 0.91-1.12 sequential |
| Interactive committed text | 4.35% | 6.17% | 1.001 PipeWire loopback |

These are engineering baselines, not universal accuracy claims. The ensemble
WER comes from deterministic 100-utterance snapshots; its RTF is the sum of the
three sequential model passes on the historical i7 benchmark host and excludes
small alignment/audit overhead. The interactive results use separate endpoint-
sized 100-utterance fixtures played through isolated PipeWire virtual nodes
into the real live-capture path on the T14. The two WER rows therefore describe
their intended workloads and are not a head-to-head comparison. Full provenance
and limitations are in the
[reproducibility report](docs/reproducibility-report.md).

## Long-form: three-model consensus

The default ensemble runs three independent full-recording transcriptions:

```text
recording
   ├── NeMo Parakeet TDT v3        (anchor and fallback)
   ├── Sherpa Parakeet Unified
   └── whisper.cpp small.en
             │
             ▼
   deterministic token alignment
             │
             ▼
      2-of-3 consensus transcript
```

Two matching token or deletion votes decide each aligned position. A three-way
disagreement falls back to the NeMo anchor and remains explicitly marked in the
audit data. No language model silently rewrites agreed text.

Set up the three native runtime images and locked models, verify the host, and
transcribe:

```bash
just setup-long-form
just doctor long-form
just ensemble recording.m4a recording.audit
```

The output directory must not already exist. On success it contains the final
transcript, all three exact model tracks, alignment decisions, disagreement
regions, logs, timing, image IDs, and model/source digests. Directories are
private (`0700`) and files are private (`0600`). See the
[long-form ensemble contract](docs/ensemble.md).

## Streaming: two-model cascade

The interactive path keeps both models loaded for the session:

```text
16 kHz mono PCM
      │
      ▼
Nemotron streaming ──► provisional revisions and phrase final
                                          │
                                          ▼
                                Parakeet phrase correction
                                          │
                                          ▼
                                  committed transcript
```

Nemotron emits genuine incremental text and endpoints phrases after 800 ms of
token silence by default. Parakeet re-decodes each finalized phrase and becomes
authoritative when it returns nonempty text within 2.5 seconds. A failure,
timeout, empty result, or full correction queue commits the Nemotron phrase with
an explicit degradation reason instead of blocking the stream.

Build the native NeMo image, fetch both models, and verify live-capture
requirements:

```bash
just setup-streaming
just doctor streaming
```

`setup-streaming` also builds the pinned release TUI. Start it only when you
intend to activate a source:

```bash
just tui
```

The TUI enumerates PipeWire sources and requires an explicit selection. It
shows the exact node name, description, physical/virtual classification, and
serial, then revalidates that identity immediately before capture. It never
falls back to the PipeWire default source. Committed text is unmarked, text
awaiting correction is labeled `… correcting`, active text is labeled
`~ provisional`, and fallback commits are labeled `! degraded`.

Use arrows or `j`/`k` to select, `r` to refresh, `a` to enter an optional new
audit destination, and `Enter` or `s` to start. While listening, `s` requests a
graceful stop. `x` opens a hard-cancellation confirmation. `q` gracefully stops
an active session and then exits; `Ctrl-C` hard-cancels and exits with status
130. `Esc` dismisses an editor, confirmation, or completed/failed session.

File replay remains a headless cascade operation; the live-only TUI does not
browse or replay recordings:

```bash
just cascade-file recording.m4a
scripts/cascade file recording.m4a --jsonl
scripts/cascade file recording.m4a --audit recording.audit
```

The authoritative headless interface remains available for integrations:

```bash
just cascade-live
scripts/cascade live --source PIPEWIRE_NODE
scripts/cascade live --source PIPEWIRE_NODE --control-stdin --jsonl
```

With `--control-stdin`, EOF before readiness exits successfully without
activating capture or publishing an audit. EOF during capture interrupts only
the recorder, closes the audio stream, and lets the supervisor flush final
text and corrections. `INT` and `TERM` remain hard cancellation: they exit 130
and never publish a successful audit.

The audit editor records only the explicit destination text. It creates
nothing itself; the wrapper still requires a nonexistent destination and an
existing writable parent, and applies its private, atomic, no-overwrite
publication contract. Persistence is disabled when the editor is blank. Raw
microphone audio is never saved. See the
[interactive cascade contract](docs/interactive-cascade.md).

## Installation and storage

The initial platform is x86-64 Linux. You need Docker, Bash, FFmpeg, standard
archive/checksum tools, and [Just](https://github.com/casey/just). Building the
TUI requires Cargo and the pinned Rust 1.97.1 toolchain. Live capture also
requires PipeWire's `pw-cat` and `pw-dump` commands. Python is used by host
orchestration and tests, but it is not present in deployed inference images.

```bash
git clone https://github.com/gracee3/native-asr.git
cd native-asr
just check
just doctor all
```

`scripts/doctor [all|long-form|streaming]` is read-only: it reports missing
tools (including Cargo, `pw-dump`, and the release TUI for streaming), an
unreachable Docker or PipeWire session, unsupported architecture,
insufficient free space, invalid locked artifacts, and missing or wrong-
architecture images. It returns `0` only when the selected workflow is ready.

Large artifacts default to a dedicated `/data` filesystem:

```bash
export NATIVE_ASR_MODELS=/data/models
export NATIVE_ASR_CACHE=/data/cache/native-asr
export NATIVE_ASR_DATASETS=/data/datasets/native-asr
export NATIVE_ASR_BENCHMARKS=/data/benchmarks/native-asr/runs.jsonl
```

All paths are overridable. Downloads are content-verified and atomically
installed. Model directories are mounted read-only at `/models`; no Dockerfile
copies or downloads weights, and every inference container uses
`--network none` and an unprivileged user.

List the complete pinned model catalog without downloading anything:

```bash
just list-models
just versions
```

## Individual models and benchmarks

The shared dispatcher can run any installed alias directly:

```bash
just transcribe nemo:parakeet-tdt-v3 recording.m4a
just transcribe moonshine:small-streaming-en recording.m4a
```

The repository also includes locked LibriSpeech/AMI datasets, WER evaluation,
single-model benchmarks, streaming tests, and staged benchmark runners. These
are research and reproducibility tools rather than a third end-user workflow:

```bash
just bench nemo:parakeet-tdt-v3 recording.m4a
just datasets
just prepare-datasets
just benchmark-set whisper:small.en librispeech-test-clean
just cascade-loopback librispeech-test-clean
```

Detailed nine-model tables, full-split checkpoints, image identities, rejected
runs, and measurement boundaries live in the documentation and published
public-corpus snapshots, not in this README.

## Documentation

- [Architecture](docs/architecture.md) — runtime boundaries and container invariants
- [Long-form ensemble](docs/ensemble.md) — consensus, output, failure, and timing contract
- [Interactive cascade](docs/interactive-cascade.md) — event protocol, scheduling, and acceptance
- [Models](docs/models.md) — lockfile, storage, and workflow roles
- [Benchmarking](docs/benchmarking.md) — WER/RTF definitions and comparison rules
- [Reproducibility report](docs/reproducibility-report.md) — complete results and limitations
- [Licensing](docs/licensing.md) — repository and third-party licensing boundaries
- [Roadmap](docs/roadmap.md) — measured next steps without changing current guarantees
- [Changelog](CHANGELOG.md) — release history, accepted results, and limitations
- [Historical i7 benchmark campaign](docs/history/2026-08-09-i7-benchmark-campaign.md)

Private recordings, transcripts, downloaded weights, and ordinary local
benchmark outputs are ignored by Git and the Docker build context. Only reviewed
snapshots derived from public evaluation corpora belong under
`benchmarks/published`.

## License

Original code, tests, and documentation in this repository are available under
the [MIT License](LICENSE). That grant does not relicense third-party native
runtimes, model weights, or datasets; consult the [licensing guide](docs/licensing.md)
and committed lockfiles before redistribution.
