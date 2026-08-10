# Experimental two-pass cascade

The recorded-file cascade is an opt-in research command. It does not replace
`scripts/transcribe` or the deterministic three-model `scripts/ensemble`
offline mode.

```bash
./scripts/cascade --output recording.cascade [--pace] recording.m4a
# or
just cascade recording.m4a recording.cascade
```

`--output` is required and must not exist. The model pair is fixed in schema 1:
`nemo:nemotron-streaming-en` is the streaming first pass and
`nemo:parakeet-tdt-v3` is the accurate second pass. Exit statuses are `0` for
success, `1` for a failure after the job started, `2` for command/preflight
validation, and `130` for cancellation.

The command verifies both locked artifacts and inspects the pinned NeMo image
before creating the measured job boundary. The container has no network, uses
an unprivileged identity and read-only root filesystem, and receives only
read-only model and source-audio mounts plus private temporary storage. The
source recording is normalized temporarily; it is never modified or copied
into the result bundle.

## Inference policy

The repository-owned native adapter loads both NeMo-Speech.cpp recognizers
exactly once in one CPU process. It feeds normalized 16 kHz mono audio to
Nemotron in exact 320-sample (20 ms) chunks and requests genuine interim
results and word offsets. Upstream token-silence endpointing is explicitly
enabled at a fixed 1,200 ms threshold. `--pace` sleeps between chunks to
reproduce the source cadence; without it, the same chunks run as quickly as
inference allows.

Only a natural token-silence endpoint or EOF finalizes a segment. There are no
microphone inputs, forced-duration cuts, overlap windows, confidence gates,
concurrent inference, or LLM stages. Consequently, continuously spoken audio
with no endpointing pause has unbounded second-pass latency until EOF. This is
an explicit version-one limitation.

At each endpoint, Parakeet runs synchronously on the exact source slice assigned
to that endpoint while its recognizer remains loaded. A valid nonempty
Parakeet hypothesis is authoritative. An error or empty hypothesis emits a
warning and selects the Nemotron final explicitly; if Nemotron is also empty,
the authoritative event records a `silence` segment. Parakeet word times are
offset from segment-relative time to source time. Nemotron's source-time word
offsets remain unchanged. The pinned runtime's EOS flush can extend a terminal
Nemotron word slightly past the endpoint range; schema 1 preserves that native
absolute span instead of clipping it, while Parakeet words remain strictly
validated inside their slice.

The final transcript is the ordered authoritative nonempty segment text joined
with one space. No spelling, punctuation, token, or confidence heuristic
rewrites it.

## Live event contract

Stdout is live JSON Lines and contains no diagnostics. Progress, native runtime
messages, and errors use stderr. Every event has:

| Field | Contract |
|---|---|
| `schema_version` | integer `1` |
| `session_id` | one UUID shared by the session |
| `sequence` | contiguous integer starting at `1` |
| `event` | one of the event types below |
| `emitted_monotonic_seconds` | nondecreasing seconds since adapter start |
| `audio_position_seconds` | nondecreasing position in source audio |

The event order is `session_started`, zero or more transcript/warning events,
`session_metrics`, and `session_completed`. A failed or cancelled stream ends
with `session_error` or `session_cancelled` when a valid terminal can be
recorded. The complete type set is:

- `session_started`
- `transcript_update`
- `session_warning`
- `session_metrics`
- `session_completed`
- `session_error`
- `session_cancelled`

A `transcript_update` contains `segment_id`, `track_id`, `revision`, `state`,
`text`, `source_time`, `model_alias`, and optional source-time `words`.
Nemotron's track is `nemotron`; its state is `provisional` or `segment_final`.
Identical consecutive provisional text is suppressed, and its revision rises
only when an update is emitted.

The `authoritative` track emits revision `1` with state `cascade_final`. It has
an exact `supersedes` reference to the segment's Nemotron final revision and a
`selection` of `parakeet`, `nemotron_fallback`, or `silence`. Segment IDs are
stable while Nemotron revises them and advance only after the cascade final.
Source ranges are contiguous and identical across both finals for a segment.
Both final updates also carry the same schema-1 `endpoint_diagnostics` object.
It records whether an automatic endpoint fired, the decoder clock, nullable
last-token clock, logical threshold-crossing clock, raw audio frontier, event
delivery position, and delivery lag. EOF finals set the endpoint clocks and
lag to `null`. These diagnostics do not change `source_time` or endpoint
behavior.

`session_metrics` is required for success. It reports one load for each fixed
model, native phase timings, segment/update/warning counts, and all selection
counts. The host rejects non-contiguous revisions, invalid transitions,
timestamp ranges, malformed JSON, provenance disagreements, missing metrics,
and output after a terminal event.

## Audit bundle

The command stages beside the destination with directory mode `0700` and file
mode `0600`, then publishes with one no-replace rename:

```text
recording.cascade/
├── result.json
├── transcript.txt
├── events.jsonl
├── segments.json
└── logs/
    └── runtime.stderr.log
```

`result.json` records the source digest/duration without copying the audio,
both locked model artifacts, inspected image ID, Git revision/dirty state,
adapter fingerprint, endpoint and pacing policies, model load counts, native
and process timings, peak RSS, segment/selection/warning counts, and artifact
references. `segments.json` retains all emitted Nemotron revisions and the
authoritative final for every exact source range. `events.jsonl` is the
validated live stream, and the log retains native diagnostics and GNU time's
measured process record.

Status is `complete`, `failed`, or `cancelled`. A pass-two fallback is a
successful, counted completion. Fatal pass one, a malformed stream, missing
terminal metrics, runtime failure, or cancellation publishes available safe
evidence but no `transcript.txt`. Cancellation signals the full container
client process group, escalates if needed, and removes the exact job container
if the engine left one behind.

## Bounded benchmark gate

The cascade-specific engineering gate is deliberately separate from the full
benchmark runners:

```bash
./scripts/benchmark-cascade-bounded
# or
just bench-cascade-bounded
```

It SHA-256-ranks at most 100 prepared utterances from each LibriSpeech split,
pairs adjacent utterances, and inserts exactly 25,600 PCM16 frames (1.6
seconds) of silence. Constructed recordings and their fingerprinted source,
digest, reference, duration, and boundary manifest live under
`$NATIVE_ASR_CACHE/cascade-bounded/`, outside the repository.

The runner first executes five unpaced cascade-only pilot pairs from each
split. A fatal runtime, event/metric/provenance error, model-load-count
mismatch, missed inserted-gap endpoint, or widespread extra endpointing stops
the pilot. Only a passing pilot proceeds to all 50 pairs per split, with each
pair evaluated by the cascade, whole-recording
Parakeet TDT, and whole-recording Nemotron in that order. Each mode and pair is
atomically checkpointed, and resume accepts only the exact cache, image,
model, adapter, Git, and option fingerprint.

`--endpoint-diagnostics-only` runs exactly those ten cascade pilots and then
stops: it launches no whole-recording baseline, full pair gate, or paced
stream. Unlike the ordinary fail-fast pilot, this mode collects every pair
despite missed or extra endpoints, while runtime and event-contract corruption
still stop it immediately. It classifies each inserted gap as
`logical_and_delivery_in_gap`, `logical_in_gap_delivery_late`,
`logical_early`, `logical_late`, or `no_natural_endpoint` with the existing
50 ms tolerance. `endpoint_diagnostics.json` reports diagnostic-collection
success separately from endpoint-contract success, per-segment clocks,
endpoint recall, delivery-lag percentiles, and `recommended_next_repair`.
The command remains nonzero when collection succeeds but the endpoint contract
fails.

`--reuse-baselines-from RUN_DIR` can import successful whole-recording
Parakeet and Nemotron details from an earlier bounded run. Reuse requires the
same cache and pair fingerprints, audio digests, model artifacts,
normalization, baseline options, and one-load contract. The runner also hashes
the NeMo CLI, every native library linked by it, and GNU time in both images;
any mismatch reruns that baseline. Imported records retain their original
timing and provenance and identify the immutable source run explicitly.

The pair gate enforces the provisional-update, segment, silence, fallback, and
relative-WER limits recorded in `pair_gate.json`. Only a passing pair gate can
create and pace the sub-300-second test-other stream. That final gate measures
inserted-gap endpoint recall, partial source-clock lag, correction latency,
fallbacks, model loads, and end-to-end RTF. Pair modes have a cumulative
120-second budget, the paced mode has a 15-minute timeout, and all work is
bounded by a 60-minute deadline.

Results default to `/data/benchmarks/native-asr/cascade/<fingerprint>/` and
include the run/cache manifests, mode and pair details, aggregate summaries,
gate verdicts, failure diagnostics, and cascade audit-bundle references. They
are external evidence, not a published benchmark snapshot. A pass means the
cascade is ready for the next engineering stage; it is not approval for
default promotion, and the command never starts a larger benchmark.
