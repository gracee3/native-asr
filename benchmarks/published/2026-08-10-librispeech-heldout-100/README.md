# LibriSpeech held-out 100-rank snapshot

This reviewed snapshot contains SHA-256 ranks 100–199 for both LibriSpeech
test splits and the three ensemble aliases. It is disjoint from the original
ranks 0–99 calibration snapshot. `snapshot.json` records the validated run and
detail digests.

| Split | Alias | Errors | Run ID |
|---|---|---:|---|
| `test-clean` | `nemo:parakeet-tdt-v3` | 45 | `20260810T141039311368Z-ee8ed7f69a9f` |
| `test-clean` | `sherpa:parakeet-unified-en` | 43 | `20260810T141312764565Z-c491b50e3124` |
| `test-clean` | `whisper:small.en` | 64 | `20260810T141533980381Z-48975e13b09e` |
| `test-other` | `nemo:parakeet-tdt-v3` | 71 | `20260810T142516356242Z-5a0fa17ef443` |
| `test-other` | `sherpa:parakeet-unified-en` | 63 | `20260810T142741342398Z-86f34264325a` |
| `test-other` | `whisper:small.en` | 151 | `20260810T142940325790Z-ff67d4fd64a9` |

All six runs completed without failures from clean implementation revision
`1b26297767c57e26d964fdc55dc3ec94498a3659`. Deterministic three-track
consensus scores 41 errors on `test-clean` and 72 on `test-other`; the snapshot
contains 27 eligible tie columns in 25 spans and 182 protected-majority
columns.
