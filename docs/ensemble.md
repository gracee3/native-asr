# Deterministic long-form ensemble

The first recorded-audio ensemble milestone is implemented as an English,
CPU-only, offline command. It runs exactly three complete model transcriptions
sequentially, aligns their lexical hypotheses, applies deterministic majority
consensus, and atomically publishes an audit bundle.

## Command

Use the reviewed default order:

```bash
./scripts/ensemble --output transcript.audit recording.m4a
# or
just ensemble recording.m4a transcript.audit
```

The default tracks are:

1. `nemo:parakeet-tdt-v3`, the alignment anchor and no-majority fallback;
2. `sherpa:parakeet-unified-en`; and
3. `whisper:small.en`.

Override the order by supplying exactly three distinct transcribable aliases:

```bash
./scripts/ensemble --output transcript.audit \
  --model whisper:small.en \
  --model nemo:parakeet-tdt-v3 \
  --model sherpa:parakeet-unified-en \
  recording.m4a
```

The first alias is always the anchor and fallback. All three models and their
required artifacts are verified before inference begins. Runtime images are
also inspected before the job boundary, and each runtime receives its validated
thread default: four threads for the pinned NeMo graph and the online logical
CPU count for Sherpa and Whisper. A runtime-managed model receives no synthetic
thread setting.

`--output` is required and must name a path that does not exist. The command
never overwrites or resumes an output path. On success it prints only the final
text to stdout; progress and errors go to stderr. Exit statuses are `0` for
success, `1` for a started job that failed, `2` for command or preflight
validation, and `130` when a started job is cancelled.

## Consensus policy

Token extraction is identical to `english-upper-apostrophe-v1` normalization,
while each token retains its source spelling, case, punctuation, and native
timing provenance. The two secondary hypotheses are aligned independently to
the first track using deterministic exact matching and local Levenshtein
alignment. Insertions from both secondary tracks are then aligned to one
another inside each anchor gap.

Every confusion-network column has three votes. A missing token is a deletion
vote. Two equal tokens select that token; two deletions remove the column. A
three-way disagreement selects the primary track's value, including a primary
deletion, and is marked unresolved. The displayed spelling, case, and
punctuation come from the first configured model that supports the selected
normalized token.

Native timing is never interpolated. NeMo word spans remain word spans. Sherpa
VAD spans and whisper.cpp transcription spans remain coarse segment spans and
may be repeated on the lexical tokens they cover with `basis: "segment"`.
Models or regions without a matching native span use `null`.

## Audit bundle

The command builds the following directory beside the requested destination,
sets directory mode `0700` and file mode `0600`, and makes it visible with one
no-replace rename:

```text
transcript.audit/
├── result.json
├── transcript.txt
├── alignment.json
├── disagreements.json
├── tracks/
│   ├── 01-….json
│   ├── 02-….json
│   └── 03-….json
└── logs/
    ├── 01-….stderr.log
    ├── 02-….stderr.log
    └── 03-….stderr.log
```

`result.json` is the bundle index. It records status, authoritative text (or
`null`), source path/digest/duration, ordered model and dependency artifacts,
container image IDs, Git state, an adapter fingerprint, per-model execution
data, aggregate timing, decision counts, and any failure.

Each track JSON wraps the exact runtime result and adds only derived normalized
tokens and their timing basis. `alignment.json` contains every confusion-network
column, including null votes, token references, vote groups, chosen surface
form, supporters, decision, and unresolved flag. `disagreements.json` groups
consecutive non-unanimous columns and records all three alternatives, the
selection, five-token context, and separate truthful time bounds for each
track. Complete per-runtime stderr, including the in-container GNU time record,
is retained under `logs/`.

`transcript.txt` exists only for a successful three-model consensus. Once a job
has started, a runtime error, malformed or empty hypothesis, missing timing or
provenance artifact, internal consensus error, or cancellation still publishes
the collected evidence with `status: "failed"` or `status: "cancelled"` and no
authoritative transcript.

## Current scope

The committed 100-utterance snapshot gate runs the default order twice and
requires identical hypotheses and no more errors than the best constituent on
both LibriSpeech splits. The current implementation produces 37/2,084 errors on
`test-clean` (best constituent: 42) and 54/1,645 on `test-other` (best
constituent: 59).

That is approximately 1.78% and 3.28% WER respectively. These deterministic
subsets are regression and engineering evidence, not a promise for every
recording. They contain independent LibriSpeech utterances rather than one
continuous meeting or interview.

The three default model passes run sequentially. Summing their historical i7
subset RTFs gives approximately 0.91 on `test-clean` and 1.12 on `test-other`,
or roughly 55-67 minutes of ASR work for one hour of comparable audio. This is
a planning estimate before the comparatively small alignment and publication
overhead, not a measured T14 end-to-end ensemble result. Exact constituent
records and comparison limits are in the
[`reproducibility report`](reproducibility-report.md).

LLM adjudication is not planned: deterministic consensus, explicit fallback,
and preserved disagreement evidence are product guarantees rather than an
intermediate stage for generated rewriting. Richer progress events, controlled
concurrency, persistent ensemble workers, and GPU scheduling are outside the
current milestone; see the [`roadmap`](roadmap.md).
