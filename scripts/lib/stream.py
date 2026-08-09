#!/usr/bin/env python3
"""Standard streaming JSONL adapter for stateful native runtimes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(os.environ["NATIVE_ASR_REPO_ROOT"])
MODELS = Path(os.environ["NATIVE_ASR_MODELS"])
LOCK = Path(os.environ.get("NATIVE_ASR_MODEL_MANIFEST", ROOT / "manifests/models.lock"))


def record(alias: str) -> dict[str, str]:
    keys = ("artifact_id", "alias", "runtime", "name", "source", "revision", "filename",
            "destination", "sha256", "license", "packaging", "requires", "notes")
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            row = dict(zip(keys, line.split("|"), strict=True))
            if row["alias"] == alias:
                return row
    raise SystemExit(f"error: unknown model alias: {alias}")


def emit(row: dict) -> None:
    print(json.dumps(row, sort_keys=True, separators=(",", ":")), flush=True)


def next_event(event: dict, previous: int, previous_emission: float) -> tuple[int, float]:
    sequence = int(event["sequence"])
    if sequence != previous + 1:
        raise RuntimeError("runtime emitted out-of-order streaming sequence")
    emission = float(event["monotonic_emission_seconds"])
    if emission < previous_emission:
        raise RuntimeError("runtime emitted decreasing monotonic time")
    return sequence, emission


def timing(stderr: str) -> tuple[float | None, float | None, int | None]:
    for line in reversed(stderr.splitlines()):
        if not line.startswith("NATIVE_ASR_TIME\t"):
            continue
        fields = line.split("\t")
        if len(fields) == 5:
            try:
                return float(fields[1]), float(fields[2]), int(fields[3])
            except ValueError:
                break
    return None, None, None


def container_user() -> str:
    if os.getuid() == 0:
        return "65532:65532"
    return f"{os.getuid()}:{os.getgid()}"


def duration(audio: Path) -> float:
    output = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(audio),
    ], text=True)
    return float(output)


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts/stream")
    parser.add_argument("model")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--update-interval-ms", type=int, default=500)
    parser.add_argument("--pace", action="store_true")
    args = parser.parse_args()
    if args.update_interval_ms <= 0:
        parser.error("--update-interval-ms must be positive")
    audio = args.audio.resolve(strict=True)
    model = record(args.model)
    if args.model not in {
        "sherpa:nemotron-streaming-en", "nemo:nemotron-streaming-en",
        "nemo:nemotron-3.5-streaming", "moonshine:small-streaming-en",
    }:
        raise SystemExit(f"error: model has no stateful streaming adapter: {args.model}")
    subprocess.run([str(ROOT / "scripts/verify-models"), args.model], check=True,
                   stdout=subprocess.DEVNULL)
    audio_seconds = duration(audio)
    started = time.monotonic()
    if model["runtime"] == "moonshine":
        runtime_root = (MODELS / "moonshine").resolve()
        audio_dir = audio.parent
        container_model = "/models/" + model["destination"].split("/", 1)[1]
        command = [
            os.environ.get("NATIVE_ASR_CONTAINER_ENGINE", "docker"), "run", "--rm",
            "--network", "none", "--user", container_user(),
            "--mount", f"type=bind,source={runtime_root},target=/models,readonly",
            "--mount", f"type=bind,source={audio_dir},target=/audio,readonly",
            "--entrypoint", "/usr/bin/time", "asr-moonshine",
            "-f", "NATIVE_ASR_TIME\\t%U\\t%S\\t%M\\t%x",
            "/usr/local/bin/native-asr-moonshine",
            "stream", "--model", container_model, "--format", "json", "--stream", "on",
            "--update-interval-ms", str(args.update_interval_ms),
        ]
        if args.pace:
            command.append("--pace")
        command.append(f"/audio/{audio.name}")
        pending_metrics: dict | None = None
        with tempfile.TemporaryFile("w+", encoding="utf-8") as runtime_stderr:
            process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                       stderr=runtime_stderr)
            assert process.stdout is not None
            last_sequence = -1
            last_emission = -1.0
            for line in process.stdout:
                event = json.loads(line)
                last_sequence, last_emission = next_event(event, last_sequence, last_emission)
                event.update({"schema_version": 1, "model_alias": args.model})
                event.setdefault("latency_ms", None)
                if event.get("event") == "stt_metrics":
                    pending_metrics = event
                else:
                    emit(event)
            status = process.wait()
            runtime_stderr.seek(0)
            stderr_text = runtime_stderr.read()
        if status:
            emit({"schema_version": 1, "event": "stt_error", "sequence": last_sequence + 1,
                  "audio_position_seconds": 0, "monotonic_emission_seconds": time.monotonic() - started,
                  "text": stderr_text[-4000:] or f"runtime exited {status}",
                  "final": True, "latency_ms": None,
                  "model_alias": args.model})
            raise SystemExit(status)
        if pending_metrics is None:
            emit({"schema_version": 1, "event": "stt_error", "sequence": last_sequence + 1,
                  "audio_position_seconds": 0,
                  "monotonic_emission_seconds": time.monotonic() - started,
                  "text": "runtime emitted no terminal metrics", "final": True,
                  "latency_ms": None, "model_alias": args.model})
            raise SystemExit(2)
        user_seconds, system_seconds, peak_rss_kb = timing(stderr_text)
        pending_metrics.update({
            "end_to_end_wall_seconds": time.monotonic() - started,
            "user_seconds": user_seconds, "system_seconds": system_seconds,
            "peak_rss_kb": peak_rss_kb,
        })
        emit(pending_metrics)
        return

    # The pinned Sherpa and NeMo CLIs expose stateful file decoding but not
    # incremental callbacks. Preserve that limitation truthfully: emit a final
    # and metrics event, never fabricated partial hypotheses.
    command = [str(ROOT / "scripts/transcribe"), "--format", "json"]
    if model["runtime"] == "nemo-speech":
        command.extend(["--stream", "on"])
    command.extend([args.model, str(audio)])
    process = subprocess.run(command, text=True, capture_output=True,
                             env={**os.environ, "NATIVE_ASR_MODEL_VERIFIED_ALIAS": args.model,
                                  "NATIVE_ASR_MEASURE": "1"})
    elapsed = time.monotonic() - started
    if process.returncode:
        emit({"schema_version": 1, "event": "stt_error", "sequence": 0,
              "audio_position_seconds": 0, "monotonic_emission_seconds": elapsed,
              "text": process.stderr[-4000:], "final": True, "latency_ms": None,
              "model_alias": args.model})
        raise SystemExit(process.returncode)
    result = json.loads(process.stdout)
    user_seconds, system_seconds, peak_rss_kb = timing(process.stderr)
    inference_elapsed = elapsed
    if args.pace and elapsed < audio_seconds:
        time.sleep(audio_seconds - elapsed)
        elapsed = time.monotonic() - started
    finalization_lag_ms = max(0.0, (elapsed - audio_seconds) * 1000) if args.pace else None
    emit({"schema_version": 1, "event": "stt_final", "sequence": 0,
          "audio_position_seconds": audio_seconds, "monotonic_emission_seconds": elapsed,
          "text": result["text"], "final": True, "latency_ms": finalization_lag_ms,
          "model_alias": args.model})
    emit({"schema_version": 1, "event": "stt_metrics", "sequence": 1,
          "audio_position_seconds": audio_seconds, "monotonic_emission_seconds": elapsed,
          "text": result["text"], "final": True, "latency_ms": finalization_lag_ms,
          "wall_seconds": elapsed, "partial_events": 0,
          "inference_wall_seconds": inference_elapsed,
          "real_time_factor": inference_elapsed / audio_seconds if audio_seconds else None,
          "user_seconds": user_seconds, "system_seconds": system_seconds,
          "peak_rss_kb": peak_rss_kb,
          "failures": 0,
          "incremental_callbacks_available": False, "model_alias": args.model})


if __name__ == "__main__":
    main()
