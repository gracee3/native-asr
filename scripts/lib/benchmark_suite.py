#!/usr/bin/env python3
"""Named, reproducible native-ASR benchmark stages."""

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
import wave

from evaluation import errors, normalize


ROOT = Path(os.environ["NATIVE_ASR_REPO_ROOT"])
DATASETS = Path(os.environ["NATIVE_ASR_DATASETS"])
CACHE = Path(os.environ["NATIVE_ASR_CACHE"])
BENCHMARKS = Path(os.environ["NATIVE_ASR_BENCHMARKS"]).parent
RUNS = Path(os.environ["NATIVE_ASR_BENCHMARKS"])
MODEL_LOCK = ROOT / "manifests/models.lock"
ENGINE = os.environ.get("NATIVE_ASR_CONTAINER_ENGINE", "docker")
ALIASES = [
    "sherpa:parakeet-unified-en", "sherpa:canary-180m-flash",
    "sherpa:nemotron-streaming-en", "nemo:parakeet-tdt-v3",
    "nemo:nemotron-streaming-en", "nemo:nemotron-3.5-streaming",
    "nemo:parakeet-ctc-1.1b", "moonshine:small-streaming-en", "whisper:small.en",
]
FINALISTS = ["sherpa:parakeet-unified-en", "nemo:parakeet-tdt-v3",
             "moonshine:small-streaming-en", "whisper:small.en"]
STREAMING = ["sherpa:nemotron-streaming-en", "nemo:nemotron-streaming-en",
             "nemo:nemotron-3.5-streaming", "moonshine:small-streaming-en"]
SPLITS = ["librispeech-test-clean", "librispeech-test-other"]
STREAM_RECIPE = "librispeech-test-other-stream-5m-v2"


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def command_output(command: list[str], default: str = "unknown") -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return default


def append_locked(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def model_row(alias: str) -> dict[str, str]:
    keys = ("artifact_id", "alias", "runtime", "name", "source", "revision", "filename",
            "destination", "sha256", "license", "packaging", "requires", "notes")
    for line in MODEL_LOCK.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            row = dict(zip(keys, line.split("|"), strict=True))
            if row["alias"] == alias:
                return row
    raise RuntimeError(f"unknown model alias: {alias}")


def run(command: list[str], output: Path | None = None) -> Path | None:
    print("+", " ".join(command), flush=True)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = output.with_name(f"{output.stem}-{stamp}{output.suffix}")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent,
                                             prefix=f".{output.stem}.", delete=False) as handle:
                temporary = Path(handle.name)
                subprocess.run(command, check=True, stdout=handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        print(f"captured: {destination}", flush=True)
        return destination
    else:
        subprocess.run(command, check=True)
        return None


def smoke() -> None:
    sample = ROOT / "samples/hf-sample1.flac"
    for alias in ALIASES:
        run([str(ROOT / "scripts/transcribe"), "--format", "json", alias, str(sample)])


def accuracy() -> None:
    for split in SPLITS:
        for alias in ALIASES:
            run([str(ROOT / "scripts/benchmark-set"), "--limit", "100", alias, split])
    for split in SPLITS:
        for alias in FINALISTS:
            run([str(ROOT / "scripts/benchmark-set"), alias, split])


def five_minute_stream() -> Path:
    source_manifest = DATASETS / "manifests/librispeech-test-other.jsonl"
    rows = [json.loads(line) for line in source_manifest.read_text().splitlines() if line]
    rows.sort(key=lambda row: (hashlib.sha256(row["utterance_id"].encode()).hexdigest(),
                               row["utterance_id"]))
    target = CACHE / "datasets/prepared/librispeech-test-other-stream-5m.wav"
    reference_path = DATASETS / "manifests/librispeech-test-other-stream-5m.json"
    if target.is_file() and reference_path.is_file():
        try:
            existing = json.loads(reference_path.read_text(encoding="utf-8"))
            if (existing.get("recipe") == STREAM_RECIPE and
                    existing.get("audio_sha256") == file_sha256(target) and
                    existing.get("source_manifest_sha256") == file_sha256(source_manifest)):
                return target
        except (OSError, json.JSONDecodeError):
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    limit_frames = 300 * 16000
    silence_frames = 4000
    selected: list[tuple[dict, bytes]] = []
    selected_frames = 0
    for row in rows:
        with wave.open(row["prepared_path"], "rb") as audio:
            if audio.getparams()[:3] != (1, 2, 16000):
                raise RuntimeError(f"prepared input is not PCM16 16 kHz mono: {row['prepared_path']}")
            frames = audio.readframes(audio.getnframes())
        frame_count = len(frames) // 2
        separator = silence_frames if selected else 0
        if selected_frames + separator + frame_count <= limit_frames:
            selected.append((row, frames))
            selected_frames += separator + frame_count
    if not selected:
        raise RuntimeError("no complete utterance fits the five-minute stream")
    total_frames = 0
    with wave.open(str(temporary), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        for index, (_, frames) in enumerate(selected):
            if index:
                output.writeframes(b"\x00\x00" * silence_frames)
                total_frames += silence_frames
            output.writeframes(frames)
            total_frames += len(frames) // 2
        trailing_frames = limit_frames - total_frames
        output.writeframes(b"\x00\x00" * trailing_frames)
        total_frames += trailing_frames
    if total_frames != limit_frames:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("five-minute stream construction produced the wrong duration")
    audio_sha256 = file_sha256(temporary)
    os.replace(temporary, target)
    reference_temporary = reference_path.with_name(reference_path.name + ".partial")
    reference_temporary.write_text(json.dumps({
        "schema_version": 2, "recipe": STREAM_RECIPE,
        "selection": "sha256(utterance_id), complete utterances only", "silence_ms": 250,
        "duration_seconds": total_frames / 16000, "trailing_silence_seconds": trailing_frames / 16000,
        "source_manifest_sha256": file_sha256(source_manifest), "audio_sha256": audio_sha256,
        "utterance_ids": [row["utterance_id"] for row, _ in selected],
        "source_sha256": [row["source_sha256"] for row, _ in selected],
        "reference": " ".join(row["reference"] for row, _ in selected),
    }, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(reference_temporary, reference_path)
    return target


def analyze_stream(path: Path, reference: str | None, identity: dict, *, long_form: bool) -> None:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    metrics = [row for row in events if row.get("event") == "stt_metrics"]
    finals = [row for row in events if row.get("event") == "stt_final"]
    partials = [row for row in events if row.get("event") == "stt_partial"]
    hypothesis = (metrics[-1].get("text", "") if metrics else
                  " ".join(row.get("text", "") for row in finals).strip())
    paced = bool(identity["options"]["pace"])
    partial_lags = ([(float(row["monotonic_emission_seconds"]) -
                      float(row["audio_position_seconds"])) * 1000 for row in partials]
                    if paced else [])
    finalization_lag = None
    if metrics and paced:
        finalization_lag = ((float(metrics[-1]["monotonic_emission_seconds"]) -
                             float(metrics[-1]["audio_position_seconds"])) * 1000)
    revisions = sum(
        normalize(partials[index].get("text", "")) != normalize(partials[index - 1].get("text", ""))
        for index in range(1, len(partials))
    )
    counts = errors(reference, hypothesis) if reference is not None else None
    sequences = [row.get("sequence") for row in events]
    emissions = [row.get("monotonic_emission_seconds") for row in events]
    ordered = (sequences == list(range(len(events))) and
               all(float(emissions[index]) >= float(emissions[index - 1])
                   for index in range(1, len(emissions))))
    event_failures = sum(row.get("event") == "stt_error" for row in events)
    runtime_failures = int(metrics[-1].get("failures", 0)) if metrics else 0
    failures = max(event_failures, runtime_failures)
    created_at = datetime.now(timezone.utc)
    run_id = f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{identity['run_fingerprint'][:12]}"
    git_revision = command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    dirty_tree = bool(command_output(["git", "-C", str(ROOT), "status", "--porcelain"], ""))
    summary = {
        **identity, "schema_version": 2, "benchmark_kind": "streaming", "run_id": run_id,
        "created_at": created_at.isoformat(), "git_revision": git_revision,
        "dirty_tree": dirty_tree, "stream_detail_path": str(path),
        "model_alias": events[0].get("model_alias") if events else None,
        "events": len(events), "partial_events": len(partials), "final_events": len(finals),
        "revisions": revisions, "failures": failures, "events_ordered": ordered,
        "mean_partial_lag_ms": sum(partial_lags) / len(partial_lags) if partial_lags else None,
        "max_partial_lag_ms": max(partial_lags) if partial_lags else None,
        "finalization_lag_ms": finalization_lag, "hypothesis_raw": hypothesis,
        "reference_raw": reference, "wer_counts": counts,
        "wer": (counts["errors"] / counts["reference_words"]
                if counts and counts["reference_words"] else None),
        "wall_seconds": metrics[-1].get("end_to_end_wall_seconds",
                                        metrics[-1].get("wall_seconds")) if metrics else None,
        "user_seconds": metrics[-1].get("user_seconds") if metrics else None,
        "system_seconds": metrics[-1].get("system_seconds") if metrics else None,
        "peak_rss_kb": metrics[-1].get("peak_rss_kb") if metrics else None,
        "real_time_factor": metrics[-1].get("real_time_factor") if metrics else None,
        "long_form_overlap_wer_reported": not long_form,
        "status": "complete" if events and metrics and ordered and failures == 0 else "failed",
    }
    destination = path.with_suffix(".summary.json")
    temporary = destination.with_name("." + destination.name + ".partial")
    temporary.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
                         encoding="utf-8")
    os.replace(temporary, destination)
    append_locked(RUNS, summary)


def capture_stream(alias: str, audio: Path, *, pace: bool, interval_ms: int,
                   output: Path, reference: str | None, long_form: bool) -> None:
    row = model_row(alias)
    image = {"sherpa-onnx": "asr-sherpa-onnx", "nemo-speech": "asr-nemo-speech",
             "moonshine": "asr-moonshine"}[row["runtime"]]
    image_id = subprocess.check_output(
        [ENGINE, "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    identity = {
        "model_alias": alias, "model_sha256": row["sha256"], "image_id": image_id,
        "audio_sha256": file_sha256(audio), "dataset_digest": file_sha256(audio),
        "reference_sha256": (hashlib.sha256(reference.encode()).hexdigest()
                              if reference is not None else None),
        "preprocessing": "pcm16-16khz-mono-v1",
        "host_adapter_sha256": hashlib.sha256(
            (ROOT / "scripts/lib/stream.py").read_bytes() +
            (ROOT / "scripts/lib/benchmark_suite.py").read_bytes()
        ).hexdigest(),
        "options": {
            "pace": pace, "update_interval_ms": interval_ms,
            "chunk_ms": 20 if row["runtime"] == "moonshine" else None,
            "streaming_mode": ("20ms-stateful-chunks" if row["runtime"] == "moonshine"
                               else "runtime-native-stateful-file"),
        },
    }
    identity["run_fingerprint"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    for prior in sorted(output.parent.glob(f"{output.stem}-*.summary.json"), reverse=True):
        try:
            existing = json.loads(prior.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (existing.get("run_fingerprint") == identity["run_fingerprint"] and
                existing.get("status") == "complete"):
            print(f"resume: {prior}", flush=True)
            return
    command = [str(ROOT / "scripts/stream")]
    if pace:
        command.append("--pace")
    command += ["--update-interval-ms", str(interval_ms), alias, str(audio)]
    detail = run(command, output)
    assert detail is not None
    analyze_stream(detail, reference, identity, long_form=long_form)


def streaming() -> None:
    stream = five_minute_stream()
    stream_reference = json.loads(
        (DATASETS / "manifests/librispeech-test-other-stream-5m.json").read_text(encoding="utf-8")
    )["reference"]
    ami_manifest = DATASETS / "manifests/ami-es2004a.jsonl"
    ami = Path(json.loads(ami_manifest.read_text().splitlines()[0])["prepared_path"])
    for alias in STREAMING:
        safe = alias.replace(":", "-").replace(".", "-")
        capture_stream(alias, stream, pace=True, interval_ms=500,
                       output=BENCHMARKS / "streams" / f"five-minute-{safe}.jsonl",
                       reference=stream_reference, long_form=False)
        capture_stream(alias, ami, pace=False, interval_ms=500,
                       output=BENCHMARKS / "streams" / f"ami-es2004a-{safe}.jsonl",
                       reference=None, long_form=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts/benchmark-suite")
    parser.add_argument("stage", choices=("smoke", "accuracy", "streaming", "staged"))
    args = parser.parse_args()
    if args.stage == "smoke":
        smoke()
    elif args.stage == "accuracy":
        accuracy()
    elif args.stage == "streaming":
        streaming()
    else:
        smoke(); accuracy(); streaming()


if __name__ == "__main__":
    main()
