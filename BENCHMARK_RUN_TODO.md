# Historical i7 benchmark campaign

> Historical status (2026-08-10): this handoff is preserved as the operational
> record for the i7-1185G7 campaign. It is no longer the active development or
> acceptance plan. Published results remain labeled i7 snapshots. The ThinkPad
> T14 i5-1145G7 with 15 GiB RAM is the sole current development and validation
> host; its interactive cascade contract is in
> `docs/interactive-cascade.md`. Do not resume this campaign as part of cascade
> acceptance.

This is the operational handoff for the agent running the completed native-ASR
suite on the larger i7 machine. The objective is to produce the real
LibriSpeech and streaming results that were intentionally not run during the
implementation pass. Do not add models or change decoder code before these
runs: a changed image, adapter, model, dataset, preprocessing recipe, or option
set creates a different benchmark fingerprint.

Status on 2026-08-09: sections 1-4 are complete on clean benchmark revision
`797eb65c3216702457b551f9308125203cc2b331`. All 18 deterministic subset rows
passed with zero failures and are published in
`docs/reproducibility-report.md`. Two section 5 cells also completed on clean
revision `44f2eafd6a6513617ead992714dab26c120b9bef`: full `test-clean` for
`sherpa:parakeet-unified-en` and `nemo:parakeet-tdt-v3`, both with 2,620
utterances and zero failures. The remaining six full-split cells start with the
other two `test-clean` finalists, followed by all four `test-other` finalists.
The Sherpa recovery changed the host-adapter fingerprint to v4, so a complete
`just bench-full` run will recompute the older v2 subset cells. The portable
operational runners are `just bench-full` and `just bench-test-clean-pair`.

## Ground rules

- Work from a clean checkout of `main` and record `git rev-parse HEAD`.
- Run benchmarks sequentially. Parallel ASR jobs would compete for CPU and RAM
  and make RTF and peak-RSS comparisons unreliable.
- Keep the machine on AC power and avoid other sustained CPU loads. Note any
  thermal throttling, power-profile changes, or interruption in the final
  report.
- Do not commit models, corpora, prepared audio, or unreviewed benchmark
  output. They belong in the external paths and are already ignored by Git.
  Only deliberately reviewed snapshots derived from public evaluation corpora
  may be published under `benchmarks/published`.
- A failed run is never resumable. A completed run is reused only when every
  fingerprint input matches. Rerunning the same command after interruption is
  safe.
- Do not report a full-meeting AMI WER. The meeting contains overlap and needs a
  separate speaker/alignment scoring protocol; this suite measures completion,
  throughput, memory, failures, and long-form stability for AMI.

## 1. Choose and preserve the external paths

The defaults are:

```bash
export NATIVE_ASR_MODELS=/data/models
export NATIVE_ASR_CACHE=/data/cache/native-asr
export NATIVE_ASR_DATASETS=/data/datasets/native-asr
export NATIVE_ASR_BENCHMARKS=/data/benchmarks/native-asr/runs.jsonl
```

If `/data` is not the large writable volume on this machine, set all four
variables to equivalent absolute paths on the large volume. Export the same
values in every new shell or tmux session. Do not change them midway through
the run.

Check both the artifact volume and Docker's separate global storage before
starting:

```bash
mkdir -p \
  "$NATIVE_ASR_MODELS" \
  "$NATIVE_ASR_CACHE" \
  "$NATIVE_ASR_DATASETS" \
  "$(dirname "$NATIVE_ASR_BENCHMARKS")"
df -h "$(dirname "$NATIVE_ASR_MODELS")"
docker info --format 'docker_root={{.DockerRootDir}}'
df -h "$(docker info --format '{{.DockerRootDir}}')"
```

Allow comfortable space for roughly 5.3 GB of installed models, retained
verified downloads, prepared datasets, four image builds, Docker build cache,
and benchmark details. Do not move Docker's global data root as part of this
task.

## 2. Establish a clean, verified runner

From the repository root:

```bash
git switch main
git pull --ff-only origin main
git status --short
git rev-parse HEAD

./scripts/check
just build
just models
just datasets
just prepare-datasets
just verify-models
just verify-datasets
```

`git status --short` must be empty before the timed work. The fetch commands are
content-addressed and resumable. Expect the initial downloads and builds to
take time; they are setup and are outside benchmark timing.

Confirm the prepared manifest counts:

```bash
wc -l \
  "$NATIVE_ASR_DATASETS/manifests/librispeech-test-clean.jsonl" \
  "$NATIVE_ASR_DATASETS/manifests/librispeech-test-other.jsonl" \
  "$NATIVE_ASR_DATASETS/manifests/ami-es2004a.jsonl"
```

Expected counts are 2,620, 2,939, and 1 respectively.

## 3. Real nine-alias smoke gate

Run this before any accuracy job:

```bash
just bench-suite smoke
```

All nine aliases must complete a real transcription. Stop and diagnose any
failure rather than beginning the long suite with a broken runtime.

## 4. Deterministic 100-utterance accuracy gate

Run all nine aliases on the SHA-256-ranked 100-utterance subset of both splits:

```bash
models=(
  sherpa:parakeet-unified-en
  sherpa:canary-180m-flash
  sherpa:nemotron-streaming-en
  nemo:parakeet-tdt-v3
  nemo:nemotron-streaming-en
  nemo:nemotron-3.5-streaming
  nemo:parakeet-ctc-1.1b
  moonshine:small-streaming-en
  whisper:small.en
)

splits=(librispeech-test-clean librispeech-test-other)

for split in "${splits[@]}"; do
  for model in "${models[@]}"; do
    ./scripts/benchmark-set --limit 100 "$model" "$split"
  done
done
```

This is the first review point. On the original machine, the same matrix was
estimated at roughly 2-3 sequential hours, but the i7 result may differ. Before
continuing, confirm that all 18 summaries have `status=complete`, zero failures,
non-null WER/RTF/RSS, and plausible transcripts.

A compact inspection command is:

```bash
jq -s '
  [.[] | select(.options.limit == 100)]
  | sort_by(.dataset, .wer, .rtf)
  | map({dataset, model_alias, run_id, status, failures, wer, rtf, peak_rss_kb})
' "$NATIVE_ASR_BENCHMARKS"
```

Do not change the four fixed finalists based on this small gate. It is mainly a
failure and gross-regression check; the committed full-run matrix remains
fixed for comparability.

## 5. Full LibriSpeech finalists

If the subset gate is clean, run both complete splits on the four fixed
finalists:

```bash
finalists=(
  sherpa:parakeet-unified-en
  nemo:parakeet-tdt-v3
  moonshine:small-streaming-en
  whisper:small.en
)

splits=(librispeech-test-clean librispeech-test-other)

for split in "${splits[@]}"; do
  for model in "${finalists[@]}"; do
    ./scripts/benchmark-set "$model" "$split"
  done
done
```

The original host's micro-run RTFs imply approximately 21 sequential hours for
this phase. Treat that only as a planning estimate. Use tmux or another durable
interactive session, and rerun the exact loop if the process is interrupted;
completed fingerprints will be skipped.

Inspect the eight full summaries separately from the 100-utterance results:

```bash
jq -s '
  [.[] | select(.options.limit == null and .benchmark_kind != "streaming")]
  | sort_by(.dataset, .wer, .rtf)
  | map({dataset, model_alias, run_id, status, failures, wer, rtf,
         peak_rss_kb, substitutions, deletions, insertions, reference_words})
' "$NATIVE_ASR_BENCHMARKS"
```

## 6. Stateful streaming and AMI

Run the committed streaming matrix:

```bash
just bench-suite streaming
```

This creates and verifies the deterministic five-minute `test-other` stream,
replays it at real-time pace with 500 ms update intervals, and then runs the
full AMI ES2004a meeting unpaced. It covers:

- `sherpa:nemotron-streaming-en`
- `nemo:nemotron-streaming-en`
- `nemo:nemotron-3.5-streaming`
- `moonshine:small-streaming-en`

Sherpa and NeMo use their stateful streaming-file paths but their pinned CLIs
do not expose incremental callbacks, so zero partial events is expected and
must not be presented as a failure. Moonshine should emit genuine partials and
revision metrics. The four paced tests alone take at least 20 minutes because
they intentionally replay five minutes per model.

Inspect the atomically published summaries:

```bash
find "$(dirname "$NATIVE_ASR_BENCHMARKS")/streams" \
  -name '*.summary.json' -type f -print0 \
  | sort -z \
  | xargs -0 -r jq -c \
      '{model_alias,run_id,status,events,partial_events,final_events,revisions,
        failures,wer,real_time_factor,mean_partial_lag_ms,finalization_lag_ms,
        peak_rss_kb,long_form_overlap_wer_reported}'
```

Expected invariants:

- every summary is complete, ordered, and has zero failures;
- five-minute runs have WER and paced latency fields;
- AMI runs have `wer=null` and `long_form_overlap_wer_reported=false`;
- CPU time, peak RSS, wall time, image ID, model/audio digests, Git revision,
  dirty-tree state, and options are present.

## 7. Resume/completeness audit

After the manual gates above succeed, run the named complete suite once:

```bash
just bench-suite staged
```

The accuracy and streaming work should report successful resumes rather than
recompute. The smoke transcriptions run again. Any unexpected new long run
means a fingerprint input changed; stop and identify the change.

## 8. Report back

Preserve the external `runs.jsonl`, `details/`, and `streams/` trees. Return a
concise report containing:

- Git revision and whether the tree stayed clean;
- CPU model, logical CPU count, RAM, kernel, Docker version, and the four image
  IDs;
- all 18 subset run IDs and all eight full-split run IDs;
- per-split WER ranking for the full four finalists, with S/D/I counts;
- RTF and peak-RSS comparisons, keeping subset and full results separate;
- streaming run IDs, failures, partial/revision behavior, paced lag, throughput,
  and peak RSS;
- any interrupted or failed run and its exact error;
- explicit confirmation that AMI WER was not reported.

Update `docs/reproducibility-report.md` only with results actually present in
the external artifacts. Keep the existing engineering micro-run clearly
separate from the new subset and full-corpus results. Do not claim rankings from
failed or partial runs, and do not push raw corpora, weights, audio, or JSONL.
