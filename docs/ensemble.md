# Long-form ensemble and bounded adjudication

The recorded-audio ensemble is English, CPU-only, and offline. It runs exactly
three complete model transcriptions sequentially, aligns their lexical
hypotheses, applies deterministic majority consensus, and atomically publishes
an audit bundle. An optional local LLM may select only existing ASR tokens or
deletions inside genuine 1-1-1 tie spans. It is disabled unless an adjudicator
alias is explicitly supplied.

## Command

Use the reviewed default order without an LLM:

```bash
./scripts/ensemble --output transcript.audit recording.m4a
just ensemble recording.m4a transcript.audit
```

The default tracks are `nemo:parakeet-tdt-v3` (alignment anchor and
no-majority fallback), `sherpa:parakeet-unified-en`, and `whisper:small.en`.
Override the order with exactly three distinct `--model ALIAS` arguments.

Enable bounded adjudication explicitly:

```bash
./scripts/ensemble --output transcript.audit \
  --adjudicator llm:ministral-3b-instruct-2512 \
  --adjudication-timeout 30 \
  recording.m4a

# The optional third Just argument has the same effect.
just ensemble recording.m4a transcript.audit llm:ministral-3b-instruct-2512
```

The timeout is per disagreement span and defaults to 30 seconds. Omitting
`--adjudicator` always produces deterministic consensus, even when an LLM model
is installed.

All configured artifacts are verified before inference. Runtime images are
inspected and the ASR tracks receive their existing validated thread defaults.
`--output` must name a path that does not exist and is never overwritten or
resumed. Success prints only final text to stdout; progress and warnings go to
stderr. Exit statuses are `0` for success, `1` for a started ASR/consensus job
that failed, `2` for command or preflight validation, and `130` for explicit
cancellation.

## Deterministic consensus

Token extraction is identical to `english-upper-apostrophe-v1` normalization,
while retaining source spelling, case, punctuation, and native timing. The two
secondary hypotheses are aligned independently to the first track with exact
matching and local Levenshtein alignment. Secondary insertions are aligned to
one another inside each anchor gap.

Every confusion-network column has three votes. A missing token is a deletion.
Two equal values win; a three-way disagreement selects the primary track and is
marked unresolved. Display spelling and punctuation come from the first model
supporting the selected normalized token. Native timing is never interpolated:
word timing stays word timing, segment timing stays explicitly coarse, and
unmatched regions use `null`.

## Tie-only bounded adjudication

Policy `primary-fallback-only-v1` sends only consecutive `primary_fallback`
columns, where all three normalized track values differ, to the LLM. Unanimous
and 2-of-3 token or deletion majorities are protected and never appear in a
request. The general disagreement artifact still reports all non-unanimous
regions. Each tie request contains five deterministic tokens of context on each
side, all three alternatives, the deterministic selection, and the exact three
candidate values for every eligible column. Transcript fields are serialized
as inert JSON data rather than interpolated into instructions.

The response must contain one decision per column. `candidate_index` is `0`,
`1`, or `2` to select that track's exact token or deletion, and `-1` to abstain.
Reasons are restricted to `contextual_fit`, `grammar`, `orthography`,
`named_entity`, `number`, or `abstain`. The host validates span/column
identities, uniqueness, completeness, bounds, and abstention consistency after
schema-constrained generation.

One invalid decision rejects the entire span. The chosen track at each tie-span
edge must also agree with the adjacent protected consensus column. A conflict
atomically restores the deterministic span and records
`boundary_path_conflict`. Prompt construction, response validation, and final
rendering all enforce the tie-only rule, so an internal caller cannot override
a majority.

One pinned `llama-server` stays resident for an enabled job. It uses four CPU
threads, a 4K context, one slot, greedy decoding, seed zero, and schema-
constrained JSON. Its container has no network, capabilities, writable root,
host audio mount, or model write access; only the selected model subtree is
mounted read-only. Jobs with no true ties verify provenance without starting
the worker, even when ordinary majority disagreements exist.

## Audit bundle

The command builds the following private directory beside the destination and
publishes it with one no-replace rename. Directories are mode `0700`; files are
mode `0600`.

```text
transcript.audit/
├── result.json
├── transcript.txt
├── consensus.txt
├── adjudication.json
├── alignment.json
├── disagreements.json
├── tracks/
│   ├── 01-….json
│   ├── 02-….json
│   └── 03-….json
└── logs/
    ├── 01-….stderr.log
    ├── 02-….stderr.log
    ├── 03-….stderr.log
    └── adjudicator.stderr.log  # only when configured
```

Schema-2 `result.json` is the bundle index. `text` is authoritative final text;
`consensus_text` preserves deterministic consensus. Its `adjudication` summary
records status, immutable model/runtime provenance, policy ID, eligible-tie and
protected-majority counts, execution metrics, and the detailed artifact
reference. Existing track, alignment, and disagreement files remain schema 1
and retain their deterministic meaning.

`adjudication.json` keeps every structured prompt, raw server response,
validated choice, timing, and explicit fallback code/reason. Its execution summary includes
load and wall time, prompt/generated tokens and throughput, span p50/p95
latency, CPU, exit status, and peak RSS. Status is one of `disabled`,
`not_needed`, `complete`, `partial`, `fallback`, or `unavailable`.

Every successful bundle contains `transcript.txt`, `consensus.txt`, and
`adjudication.json`. LLM startup failure, crash, malformed output, invalid
choice, or timeout warns, preserves consensus for affected spans, and still
exits zero. Explicit cancellation remains a cancelled job without an
authoritative transcript. ASR, provenance, and consensus failures retain their
existing failed-bundle behavior.

## Validation and remaining scope

The deterministic 200-utterance regression remains unchanged when adjudication
is disabled: 37/2,084 errors on `test-clean` and 54/1,645 on `test-other`.
Candidate-selection oracle ceilings are 27 and 40 errors respectively. Real
model bake-off evidence and a recommended long-form alias are published only
after both repeated-decision and accuracy gates pass.

The earlier bounded-span result remains historical evidence. The tie-only
experiment evaluates fresh prompts twice on both the original calibration
snapshot and a disjoint held-out snapshot before making any recommendation.

Continuous/provisional streaming adjudication, invented-text reconstruction,
concurrent ASR scheduling, persistent ASR workers, and GPU scheduling remain
deferred. The worker protocol is designed to be reused later only at finalized
streaming segment boundaries.
