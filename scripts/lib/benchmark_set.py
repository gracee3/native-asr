#!/usr/bin/env python3
"""Reproducible accuracy/performance evaluation for one model and dataset."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from batch_adapter import run_batch
from evaluation import NORMALIZATION_VERSION, errors, normalize


ROOT = Path(os.environ["NATIVE_ASR_REPO_ROOT"])
MODELS = Path(os.environ["NATIVE_ASR_MODELS"])
CACHE = Path(os.environ["NATIVE_ASR_CACHE"])
DATASETS = Path(os.environ["NATIVE_ASR_DATASETS"])
RUNS = Path(os.environ["NATIVE_ASR_BENCHMARKS"])
MANIFEST = Path(os.environ.get("NATIVE_ASR_MODEL_MANIFEST", ROOT / "manifests/models.lock"))
ENGINE = os.environ.get("NATIVE_ASR_CONTAINER_ENGINE", "docker")


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def adapter_digest() -> str:
    value = hashlib.sha256()
    for name in ("batch_adapter.py", "benchmark_set.py", "evaluation.py"):
        value.update((ROOT / "scripts/lib" / name).read_bytes())
    return value.hexdigest()


def model_record(alias: str) -> dict[str, str]:
    keys = ("artifact_id", "alias", "runtime", "name", "source", "revision", "filename",
            "destination", "sha256", "license", "packaging", "requires", "notes")
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            row = dict(zip(keys, line.split("|"), strict=True))
            if row["alias"] == alias:
                return row
    raise SystemExit(f"error: unknown model alias: {alias}")


def command_output(command: list[str], default: str = "unknown") -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return default


def load_manifest(dataset: str) -> tuple[Path, list[dict]]:
    path = DATASETS / "manifests" / f"{dataset}.jsonl"
    if not path.is_file():
        raise SystemExit(f"error: prepared dataset manifest is missing: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return path, rows


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def previous_complete(run_fingerprint: str) -> dict | None:
    if not RUNS.is_file():
        return None
    for line in reversed(RUNS.read_text(encoding="utf-8").splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("run_fingerprint") == run_fingerprint and row.get("status") == "complete":
            return row
    return None


def append_locked(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts/benchmark-set")
    parser.add_argument("model")
    parser.add_argument("dataset")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--threads", type=int)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.threads is not None and args.threads <= 0:
        parser.error("--threads must be positive")

    record = model_record(args.model)
    if record["runtime"] == "moonshine" and args.threads is not None:
        parser.error("Moonshine manages its ONNX Runtime thread pool; --threads is not configurable")
    if record["runtime"] == "nemo-speech" and args.threads not in (None, 4):
        parser.error("the pinned NeMo-Speech.cpp ASR runtime uses exactly four threads")
    subprocess.run([str(ROOT / "scripts/verify-models"), args.model], check=True,
                   stdout=subprocess.DEVNULL)
    dataset_manifest, utterances = load_manifest(args.dataset)
    if not utterances:
        raise SystemExit(f"error: prepared dataset manifest is empty: {dataset_manifest}")
    utterances.sort(key=lambda row: (hashlib.sha256(row["utterance_id"].encode()).hexdigest(),
                                     row["utterance_id"]))
    if args.limit:
        utterances = utterances[:args.limit]
    image = {"sherpa-onnx": "asr-sherpa-onnx", "nemo-speech": "asr-nemo-speech",
             "moonshine": "asr-moonshine", "whisper-cpp": "asr-whisper-cpp"}[record["runtime"]]
    image_id = command_output([ENGINE, "image", "inspect", image, "--format", "{{.Id}}"])
    git_revision = command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    dirty = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet"]).returncode != 0 or bool(
        command_output(["git", "-C", str(ROOT), "status", "--porcelain"], ""))
    threads = (1 if record["runtime"] == "moonshine" else
               (args.threads or (4 if record["runtime"] == "nemo-speech" else (os.cpu_count() or 1))))
    options = {"threads": "runtime-managed" if record["runtime"] == "moonshine" else threads,
               "normalization": NORMALIZATION_VERSION,
               "subset": "sha256(utterance_id)", "limit": args.limit}
    identity = {
        "schema_version": 2, "image_id": image_id, "model_alias": args.model,
        "model_sha256": record["sha256"], "dataset": args.dataset,
        "dataset_manifest_sha256": sha(dataset_manifest), "preprocessing": "pcm16-16khz-mono-v1",
        "host_adapter_sha256": adapter_digest(),
        "options": options,
    }
    run_fingerprint = fingerprint(identity)
    if not args.no_resume and (existing := previous_complete(run_fingerprint)):
        print(f"resume: {existing['run_id']} already complete ({args.model} / {args.dataset})")
        return

    created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{created}-{run_fingerprint[:12]}"
    details_dir = RUNS.parent / "details"
    details_dir.mkdir(parents=True, exist_ok=True)
    destination = details_dir / f"{run_id}.jsonl"
    totals = {"substitutions": 0, "deletions": 0, "insertions": 0,
              "reference_words": 0, "errors": 0, "failures": 0}
    audio_seconds = 0.0
    # One cold probe makes startup + model load + first inference visible.
    probe_command = [str(ROOT / "scripts/transcribe"), "--format", "json"]
    if record["runtime"] != "moonshine":
        probe_command += ["--threads", str(threads)]
    probe_command += [args.model, utterances[0]["prepared_path"]]
    probe_started = time.monotonic()
    probe = subprocess.run(probe_command, text=True, capture_output=True,
                           env={**os.environ, "NATIVE_ASR_MODEL_VERIFIED_ALIAS": args.model})
    cold_probe_wall = time.monotonic() - probe_started
    if probe.returncode:
        raise SystemExit(f"error: cold transcription probe failed: {probe.stderr[-4000:]}")
    batch = run_batch(record, utterances, MODELS, CACHE, threads)
    wall_total = batch.wall_seconds
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=details_dir,
                                         prefix=f".{run_id}.", delete=False) as output:
            temporary = Path(output.name)
            for sequence, utterance in enumerate(utterances):
                raw_hypothesis = batch.hypotheses.get(utterance["utterance_id"], "")
                failure = batch.failures.get(utterance["utterance_id"])
                audio_seconds += float(utterance["duration_seconds"])
                if failure is None:
                    counts = errors(utterance["reference"], raw_hypothesis)
                    for key in ("substitutions", "deletions", "insertions",
                                "reference_words", "errors"):
                        totals[key] += counts[key]
                else:
                    totals["failures"] += 1
                    counts = {"substitutions": 0, "deletions": 0, "insertions": 0,
                              "reference_words": len(normalize(utterance["reference"]).split()),
                              "errors": 0}
                    totals["reference_words"] += counts["reference_words"]
                detail = {
                    "schema_version": 2, "run_id": run_id, "sequence": sequence,
                    "utterance_id": utterance["utterance_id"], "split": utterance["split"],
                    "source_path": utterance["source_path"],
                    "prepared_path": utterance["prepared_path"],
                    "source_sha256": utterance["source_sha256"],
                    "duration_seconds": utterance["duration_seconds"],
                    "speaker": utterance["speaker"], "reference_raw": utterance["reference"],
                    "hypothesis_raw": raw_hypothesis,
                    "reference_normalized": normalize(utterance["reference"]),
                    "hypothesis_normalized": normalize(raw_hypothesis), "wer_counts": counts,
                    "wall_seconds": None, "exit_status": 0 if failure is None else 2,
                    "stderr": failure,
                }
                output.write(json.dumps(detail, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    wer = (None if totals["failures"] else
           ((totals["substitutions"] + totals["deletions"] + totals["insertions"])
            / totals["reference_words"] if totals["reference_words"] else None))
    summary = {
        **identity, "run_fingerprint": run_fingerprint, "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if totals["failures"] == 0 else "failed",
        "runtime": record["runtime"], "model_name": record["name"],
        "dataset_digest": identity["dataset_manifest_sha256"], "git_revision": git_revision,
        "dirty_tree": dirty, "detail_path": str(destination), "utterances": len(utterances),
        "total_audio_seconds": audio_seconds, "wall_seconds": wall_total,
        "cold_start_model_load_and_first_inference_seconds": cold_probe_wall,
        "batch_wall_seconds": batch.wall_seconds,
        "batch_model_loads": 1, "batch_model_reused_for_utterances": len(utterances),
        "user_seconds": batch.user_seconds, "system_seconds": batch.system_seconds,
        "peak_rss_kb": batch.peak_rss_kb, "rtf": wall_total / audio_seconds if audio_seconds else None,
        "wer": wer, **totals,
    }
    append_locked(RUNS, summary)
    print(f"run: {run_id}  model: {args.model}  dataset: {args.dataset}")
    print(f"utterances: {len(utterances)}  failures: {totals['failures']}  WER: {wer if wer is not None else 'n/a'}")
    print(f"RTF: {summary['rtf'] if summary['rtf'] is not None else 'n/a'}  details: {destination}")
    if totals["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
