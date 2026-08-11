# Reproducibility report

## Host scope

All results dated 2026-08-09 below are preserved historical i7-1185G7
snapshots. They remain useful for their recorded benchmark fingerprints but are
not evidence for the current interactive milestone. Beginning 2026-08-10, the
ThinkPad T14 i5-1145G7 with 15 GiB RAM is the sole development and acceptance
host for the Nemotron-to-Parakeet cascade. Reviewed T14 cascade aggregates are
recorded below; raw event logs remain outside Git.

## Current long-form ensemble baseline

The current deterministic consensus implementation is replayed in repository
tests over the three default tracks in the published 2026-08-09 100-utterance
snapshot. It produces byte-identical repeated hypotheses and improves on the
best constituent in both splits:

| Split | Consensus errors / words | Consensus WER | Best constituent errors / words |
|---|---:|---:|---:|
| `test-clean` | 37 / 2,084 | 1.78% | 42 / 2,084 |
| `test-other` | 54 / 1,645 | 3.28% | 59 / 1,645 |

Those WERs are derived by aligning the exact published NeMo Parakeet, Sherpa
Parakeet, and whisper.cpp hypotheses per utterance. They are deterministic
snapshot/regression evidence, not a separately timed continuous-audio run.

The default command executes the three model passes sequentially. Summing their
published constituent RTFs gives `0.162 + 0.131 + 0.615 = 0.908` for
`test-clean` and `0.162 + 0.136 + 0.824 = 1.122` for `test-other`. The README
therefore reports an approximate 0.91-1.12 long-form RTF, before alignment and
audit publication overhead. These are historical i7 planning estimates; a
complete end-to-end T14 ensemble measurement remains a roadmap item.

## 2026-08-11 accepted T14 PipeWire cascade

The CPU-only interactive cascade passed on an 11th Gen Intel Core i5-1145G7
(four cores, eight logical CPUs), 16,454,795,264 bytes of RAM, Linux
`7.0.0-28-generic`, Docker `29.7.2`, and PipeWire 1.0.5. The release NeMo image
is `sha256:a170c7810eb41f5d5f3ec5fb7022709aa3bf6276a04964473c98a76665217250`,
200,683,478 bytes, configured as `65532:65532`. Models were verified on the
host, mounted read-only at `/models`, and run with `--network none`.

The interactive fixture is distinct from the historical batch subset. For each
split it ranks complete utterances by `abs(duration_seconds - 3.0)`, then by
SHA-256 of `utterance_id`, and selects the first 100. Schema 2 inserts 1,000 ms
between utterances plus 1,000 ms at each boundary so startup scheduling cannot
truncate speech:

| Split | Speech range / seconds | Reference words / speakers | Total seconds | Fixture fingerprint | WAV SHA-256 |
|---|---:|---:|---:|---|---|
| `test-clean` | 2.885-3.110 / 300.550 | 759 / 31 | 401.550 | `fbdd1075b8c15482ff54036890343d14b498ba8f0d4ace1daa2c0f8102ff8344` | `6566df256de29100308f7ce33cbf43a763c8d7fdf0cdcb1bcf3fca163c31551c` |
| `test-other` | 2.900-3.100 / 300.490 | 810 / 28 | 401.490 | `4e4a1f38bb0ed3505e2d757ba1e1c70ac61869cd2d71d9dfaa592156577bfc15` | `a6965170c4b9cfe0899e0964e6e8db32598671f789ab792bec9ad11c07be13f6` |

Both runs used the default 800 ms endpoint, one 160 ms RNNT right-context
frame, 880 ms token-to-acoustic shift, 320 ms acoustic tail, 2.5 second
correction deadline, one active correction, and one waiting correction. WER
uses `english-upper-apostrophe-v1`.

### PipeWire-loopback release acceptance

| Split | External run ID | Segments / events | Nemotron WER | Committed WER | Corrected / rate | Churn | Degraded | Partial p95 | Correction p95 / max | RTF | CPU user+sys s | Peak RSS KiB | Package peak |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `test-clean` | `t14-v0.1.0-rerun-librispeech-test-clean-100-loopback` | 104 / 1,158 | 4.87% | 4.35% | 29 / 27.88% | 4.77% | 0 | 142 ms | 635 / 847 ms | 1.00076 | 854.126 | 1,735,916 | 100 C |
| `test-other` | `t14-v0.1.0-rerun-librispeech-test-other-100-loopback` | 106 / 1,151 | 7.65% | 6.17% | 46 / 43.40% | 9.15% | 0 | 138 ms | 614 / 1,002 ms | 1.00081 | 852.049 | 1,736,400 | 98 C |

Both summaries record clean revision
`8f6ef4e4a2fcf7e3e5ed5a7fea164353ae4a57d2`. All 13 gates passed: contiguous
events, ordered commits, zero degraded segments, p95 partial lag at most 750 ms,
every correction within 2.5 seconds, committed WER no worse than Nemotron, RTF
from 0.98 through 1.10, one load per model, no swap growth, complete playback
and capture, no physical-device link, and complete virtual cleanup.

The clean run used sink/source object IDs 80/119 with serials 3420/3419; the
other run used IDs 93/123 with serials 3443/3442. Node names had independent
randomized `native_asr_loopback_` prefixes. Graph inspections at creation,
capture, simultaneous playback/capture, and post-playback found no physical or
unknown endpoint. Both `pw-play` and bounded capture returned zero, `pw-loopback`
exited cleanly, and post-cleanup inspection found neither virtual node. No
captured PCM was written.

The package temperature maxima are reported rather than hidden; there was no
thermal acceptance threshold. Audit directories are private and contain
events, results, and committed text only. Raw events, audits, and generated
public-corpus WAV fixtures remain external to Git.

### Rejected run and regression evidence

The first full loopback attempt,
`t14-v0.1.0-librispeech-test-clean-100-loopback`, was rejected because a valid
400 ms second endpoint for utterance `1089-134691-0018` produced an empty
Parakeet correction and one degraded segment. The fix extended only short
correction input with already buffered real context and added a bounded silence-
padding retry. The specific utterance passed file and real virtual-source tests,
then both complete fixtures were rerun from the clean revision reported above.

Earlier file-transport paced and unpaced runs remain regression and CPU-pressure
evidence, not release acceptance. The unpaced RTFs were 0.415 and 0.444. A
broader `--selection hash` clean fixture was also rejected as interactive
acceptance evidence because long audiobook utterances split into 187 mid-
sentence regions, worsened committed WER, and produced empty corrections. Hash
selection remains an explicit stress test, while the three-model recorded-audio
ensemble remains the supported long-form path.

## 2026-08-09 validated i7 state

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
| `asr-sherpa-onnx` | `sha256:1deb3ee342cd7aadb02f0b408dfaabd33e7775409378df698066a4ab15f7072a` | 208,228,994 | `65532:65532` |
| `asr-nemo-speech` | `sha256:c77deabc18bbe649288161e2bb4e6660fbdf8c00a291d799bf9ac3439e65917c` | 200,586,441 | `65532:65532` |
| `asr-moonshine` | `sha256:8c057ded9d4b3ba99fa5027ba655546a6213b37eb1f89a5c744a3dc31dc6cf38` | 214,392,015 | `65532:65532` |
| `asr-whisper-cpp` | `sha256:fc0255d232ffe39294057f8a5e1cdd15625d651198891204fd4da1f0a56f2ab2` | 199,118,725 | `65532:65532` |

The benchmark host was an 11th Gen Intel Core i7-1185G7 with four physical
cores, eight logical CPUs, 33,365,962,752 bytes of RAM, Linux
`7.0.0-28-generic`, and Docker `29.7.2`. It remained on AC power in the
performance profile. A pre-validation unbounded Sherpa batch caused thermal
throttling and was rejected; none of its measurements appear below.

## Deterministic 100-utterance subset validation

All 18 final-fingerprint runs completed with zero failures on clean Git
revision `797eb65c3216702457b551f9308125203cc2b331`. Each split independently
uses the first 100 utterance IDs ranked by SHA-256. WER uses
`english-upper-apostrophe-v1`; RTF is batch wall time divided by prepared audio
duration and excludes the separately recorded cold-start probe.

### LibriSpeech `test-clean`

| Model | Run ID | WER | RTF | Peak RSS KiB |
|---|---|---:|---:|---:|
| `sherpa:parakeet-unified-en` | `20260809T174303153388Z-5a6a2972ab40` | 4.46% | 0.131 | 3,664,144 |
| `sherpa:canary-180m-flash` | `20260809T174452951202Z-e41c28c2a9fd` | 7.25% | 0.101 | 733,564 |
| `sherpa:nemotron-streaming-en` | `20260809T174617510134Z-2b95027ab002` | 6.38% | 0.112 | 7,180,160 |
| `nemo:parakeet-tdt-v3` | `20260809T174753072178Z-cc8b31d49ecf` | 2.02% | 0.162 | 959,144 |
| `nemo:nemotron-streaming-en` | `20260809T175007319530Z-d4310534049b` | 2.59% | 0.297 | 962,084 |
| `nemo:nemotron-3.5-streaming` | `20260809T180139075576Z-02a51ed21e73` | 4.17% | 0.334 | 960,136 |
| `nemo:parakeet-ctc-1.1b` | `20260809T180615628321Z-d2a8f7ecf818` | 2.45% | 0.280 | 1,570,188 |
| `moonshine:small-streaming-en` | `20260809T181019844622Z-8ced8e8f09c7` | 6.33% | 0.297 | 702,112 |
| `whisper:small.en` | `20260809T181423306432Z-da590fc7b8fb` | 2.88% | 0.615 | 777,632 |

### LibriSpeech `test-other`

| Model | Run ID | WER | RTF | Peak RSS KiB |
|---|---|---:|---:|---:|
| `sherpa:parakeet-unified-en` | `20260809T182250705320Z-fb8180c893b5` | 3.59% | 0.136 | 3,674,632 |
| `sherpa:canary-180m-flash` | `20260809T182413725398Z-d1b2a462ad85` | 4.07% | 0.108 | 635,648 |
| `sherpa:nemotron-streaming-en` | `20260809T182519568158Z-85595fa8012b` | 9.73% | 0.116 | 7,120,244 |
| `nemo:parakeet-tdt-v3` | `20260809T182631716247Z-cb74fe9a87a7` | 4.26% | 0.162 | 904,440 |
| `nemo:nemotron-streaming-en` | `20260809T182809398825Z-d83e7c4a0bf5` | 6.26% | 0.322 | 962,016 |
| `nemo:nemotron-3.5-streaming` | `20260809T183122525387Z-3f6b60459ca6` | 9.67% | 0.320 | 960,432 |
| `nemo:parakeet-ctc-1.1b` | `20260809T183434978852Z-a9b1157407c4` | 4.13% | 0.276 | 1,511,132 |
| `moonshine:small-streaming-en` | `20260809T183730608520Z-cd1cd28aa34f` | 14.10% | 0.298 | 689,268 |
| `whisper:small.en` | `20260809T184027681230Z-e833b1a5320b` | 7.29% | 0.824 | 777,456 |

These subsets are a reproducible failure and gross-regression gate, not a
full-split ranking. Most runtime batches load once and reuse the model for the
remaining 99 utterances. Sherpa Parakeet instead used its fingerprinted bounded
policy: 19 loads on `test-clean` and nine on `test-other`, with utterances over
20 seconds routed through the pinned Silero VAD path. NeMo CTC's policy
canonicalized three native non-standard `-nan` aggregate confidence values to
JSON `null` on `test-clean`; transcript text was unaffected and all 100 strict
records were validated.

## Initial full-split `test-clean` validation

Two requested finalist cells completed on clean Git revision
`44f2eafd6a6513617ead992714dab26c120b9bef`. Both used the complete locked
2,620-utterance `librispeech-test-clean` manifest: 19,452.481 seconds (5.403
hours) and 52,576 normalized reference words. All 5,240 detail records have
`exit_status=0`, no failure text, and a mapped hypothesis. RTF excludes the
separately recorded cold-start probe, as in the subset table.

| Model | Run ID | WER | S / D / I | RTF | Wall min | Peak RSS KiB | Loads |
|---|---|---:|---:|---:|---:|---:|---:|
| `sherpa:parakeet-unified-en` | `20260809T210123493196Z-ab7728fffe58` | 2.12% | 809 / 206 / 101 | 0.141 | 45.56 | 3,836,880 | 378 |
| `nemo:parakeet-tdt-v3` | `20260809T214700876989Z-7fed3a8f2e1c` | 2.15% | 915 / 97 / 119 | 0.160 | 52.01 | 1,036,020 | 1 |

This is a two-model checkpoint, not the final four-finalist ranking. Sherpa is
slightly lower in WER and RTF in these two rows, while NeMo uses substantially
less peak memory and one model load. The other two `test-clean` finalists and
all four `test-other` cells have not been run under this full-split checkpoint.

The first official Sherpa attempt,
`20260809T195818147535Z-f177d0fa99bc`, is preserved only in the external
append-only ledger with `status=failed`: 44 of 2,620 inputs were missing after
four exit-zero, all-empty upstream batches. Its WER is null and it is not in the
published snapshot. Real-audio reproduction showed that recursive group
isolation plus the pinned VAD recovered 42 inputs; the remaining two transcribed
only after lossless balanced segmentation. The committed v4 adapter therefore
recursively bisects exit-zero empty or malformed groups, retries affected
singletons with VAD, and uses balanced PCM chunks of at most 10 seconds only if
VAD is also empty. Every retry process contributes to wall/CPU time, peak RSS,
and the 378-load count; any nonzero process or empty final fallback still fails
closed. Unit/smoke checks and a focused 44/44 real-audio replay passed before the
clean official rerun.

The exact successful aggregate and detail records are published in
[`benchmarks/published/2026-08-09-librispeech-test-clean-pair`](../benchmarks/published/2026-08-09-librispeech-test-clean-pair/README.md).

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

## Earlier engineering streaming validation

The streaming results in this section predate the clean `797eb65` subset
matrix and are retained as engineering validation, not final staged-suite
results.

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

`just bench-suite staged` implements the complete deterministic matrix:
both 100-utterance LibriSpeech subsets across nine aliases, full splits across
four finalists, the five-minute paced stream, and unpaced full AMI streaming.
It is resumable only on an exact runtime image, model, dataset, preprocessing,
adapter, and options fingerprint, and it never reports an overlap-sensitive AMI
full-meeting WER.

The 18-row subset phase completed and its gate passed on revision `797eb65`.
The targeted recovery run later completed two of eight full-split cells on the
v4 adapter at revision `44f2eaf`: Sherpa Parakeet and NeMo Parakeet TDT v3 on
full `test-clean`. Six full-split cells, final-revision paced streaming, and AMI
remain pending. The v4 adapter changes the host-adapter fingerprint, so a new
`just bench-suite staged` run will recompute the older subset cells rather than
silently treat their v2 records as current. This report consequently publishes
the subset gate and two-model full checkpoint separately and makes no purported
four-finalist full-split ranking.

The official Moonshine v0.1.1 Small Streaming component catalog currently
installs 247,255,694 bytes across eight locked files, rather than the earlier
approximate 165 MB planning figure. The lock records the observed file sizes and
digests, and that verified upstream tree is what these results used.
