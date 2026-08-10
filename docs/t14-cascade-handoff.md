# ThinkPad T14 cascade handoff

Status as of 2026-08-10: the logical endpoint-boundary repair is implemented on
`experiment/two-pass-cascade`, and the code-bearing commit `388dcfa` is pushed.
The branch has no unpublished source changes.

## Verified state

- `./scripts/check` and `git diff --check` passed.
- A no-cache NeMo image rebuild passed on the source host.
- The exact ten-pair `--endpoint-diagnostics-only` replay passed using cache
  `3df2c46ac4c70302fa0276623b7e355fb10725499b8b861923c69135af09d8ad`.
- Source-boundary recall was 10/10, with one extra-endpoint pair out of two
  allowed. Delivery classification remained seven
  `logical_and_delivery_in_gap` and three `logical_in_gap_delivery_late`.
- All ten adapter-owned PCM partitions were sample-contiguous and ended at EOF.
- Verified adapter fingerprints are `9372c98616cd6d8c23f9187143fb6601311d4bf178716044e04c0b8e2adb67f9`
  for the bounded runner and `9b87cbda032f9d4c3c04aed6fd69535ac38025f39f107d779f885d2fccc835a9`
  for the cascade adapter.

The source-host evidence is external at
`/data/benchmarks/native-asr/cascade/e181cb027def0e1b72a71fb01eaa5a0ea2b0ad56d335e313462d5717862a521e`.
It records Git commit `388dcfa` and image
`sha256:c13836fff6a45beddc68ac8c52bc443d2594984407b35fa873b6a6b6b7768289`.
It is intentionally not committed to Git.

## Resume on the T14

```bash
git fetch origin
git switch experiment/two-pass-cascade || \
  git switch --track origin/experiment/two-pass-cascade
git pull --ff-only
git merge-base --is-ancestor 388dcfa HEAD
./scripts/check
docker build --no-cache --file docker/nemo-speech/Dockerfile --tag asr-nemo-speech .
./scripts/verify-models nemo:nemotron-streaming-en nemo:parakeet-tdt-v3
```

If the model or dataset material is not copied from this host, reproduce it with:

```bash
./scripts/models fetch nemo:nemotron-streaming-en nemo:parakeet-tdt-v3
./scripts/datasets fetch librispeech-test-clean librispeech-test-other
./scripts/datasets prepare librispeech-test-clean librispeech-test-other
```

To preserve the exact cached inputs and evidence instead, copy these external
trees to the same absolute paths on the T14:

- `/data/datasets/native-asr/`
- `/data/cache/native-asr/datasets/prepared/`
- `/data/cache/native-asr/cascade-bounded/3df2c46ac4c70302fa0276623b7e355fb10725499b8b861923c69135af09d8ad/`
- `/data/models/nemo-speech/nemotron-streaming-en/`
- `/data/models/nemo-speech/parakeet-tdt-v3/`
- optionally, the evidence directory listed above

The diagnostic can be reproduced—without starting the larger gate—with:

```bash
./scripts/benchmark-cascade-bounded \
  --cache-root /data/cache/native-asr/cascade-bounded \
  --endpoint-diagnostics-only
```

## Deliberate stopping point

No 100-pair gate, whole-recording baselines, paced stream, PR, or published
snapshot has been run for this repair. The ten-pair result validates source
ownership and delivery-clock preservation; it is not yet a WER or production
readiness result. Choose and authorize the next gate before launching it.
