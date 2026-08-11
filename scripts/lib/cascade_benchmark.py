#!/usr/bin/env python3
"""Build and evaluate a deterministic public-corpus cascade replay."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import threading
import time
import wave

from evaluation import NORMALIZATION_VERSION, errors, normalize
from pipewire_loopback import PipeWireError, PipeWireLoopback, terminate_owned_process


ROOT = Path(os.environ["NATIVE_ASR_REPO_ROOT"])
CACHE = Path(os.environ["NATIVE_ASR_CACHE"])
DATASETS = Path(os.environ["NATIVE_ASR_DATASETS"])
BENCHMARKS = Path(os.environ["NATIVE_ASR_BENCHMARKS"]).parent


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as output:
        temporary = Path(output.name)
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def selected_rows(dataset: str, limit: int, selection: str) -> tuple[Path, list[dict]]:
    manifest = DATASETS / "manifests" / f"{dataset}.jsonl"
    if not manifest.is_file():
        raise SystemExit(f"error: prepared dataset manifest is missing: {manifest}")
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    if selection == "phrase":
        rows.sort(
            key=lambda row: (
                abs(float(row["duration_seconds"]) - 3.0),
                hashlib.sha256(row["utterance_id"].encode()).hexdigest(),
                row["utterance_id"],
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                hashlib.sha256(row["utterance_id"].encode()).hexdigest(), row["utterance_id"]
            )
        )
    if len(rows) < limit:
        raise SystemExit(f"error: {dataset} has only {len(rows)} prepared utterances")
    return manifest, rows[:limit]


def fixture(
    dataset: str, manifest: Path, rows: list[dict], silence_ms: int,
    selection: str = "phrase", boundary_silence_ms: int = 1000,
) -> tuple[Path, dict]:
    identity = {
        "schema_version": 2,
        "dataset": dataset,
        "manifest_sha256": digest(manifest),
        "utterance_ids": [row["utterance_id"] for row in rows],
        "selection": (
            "abs(duration_seconds-3.0),sha256(utterance_id)" if selection == "phrase"
            else "sha256(utterance_id)"
        ),
        "silence_ms": silence_ms,
        "boundary_silence_ms": boundary_silence_ms,
        "format": "pcm16le-16000hz-mono",
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    directory = CACHE / "cascade-fixtures"
    audio = directory / f"{dataset}-{len(rows)}-{fingerprint[:12]}.wav"
    metadata = audio.with_suffix(".json")
    if audio.is_file() and metadata.is_file():
        previous = json.loads(metadata.read_text(encoding="utf-8"))
        if previous.get("fixture_fingerprint") == fingerprint and previous.get("sha256") == digest(audio):
            return audio, previous

    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{audio.name}.", delete=False) as handle:
        temporary = Path(handle.name)
    frames = 0
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            silence = b"\0\0" * (16000 * silence_ms // 1000)
            boundary_silence = b"\0\0" * (16000 * boundary_silence_ms // 1000)
            output.writeframesraw(boundary_silence)
            frames += len(boundary_silence) // 2
            for index, row in enumerate(rows):
                source = Path(row["prepared_path"])
                with wave.open(str(source), "rb") as current:
                    if (
                        current.getnchannels() != 1
                        or current.getsampwidth() != 2
                        or current.getframerate() != 16000
                        or current.getcomptype() != "NONE"
                    ):
                        raise RuntimeError(f"unexpected prepared WAV format: {source}")
                    data = current.readframes(current.getnframes())
                    output.writeframesraw(data)
                    frames += len(data) // 2
                if index + 1 < len(rows):
                    output.writeframesraw(silence)
                    frames += len(silence) // 2
            output.writeframesraw(boundary_silence)
            frames += len(boundary_silence) // 2
        os.replace(temporary, audio)
    finally:
        temporary.unlink(missing_ok=True)
    value = {
        **identity,
        "fixture_fingerprint": fingerprint,
        "path": str(audio),
        "sha256": digest(audio),
        "frames": frames,
        "duration_seconds": frames / 16000,
    }
    atomic_json(metadata, value)
    return audio, value


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def swap_used_kb() -> int:
    fields: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, value, *_ = line.replace(":", "").split()
        if name in {"SwapTotal", "SwapFree"}:
            fields[name] = int(value)
    return fields.get("SwapTotal", 0) - fields.get("SwapFree", 0)


def temperature_paths() -> list[Path]:
    candidates = {
        Path(path)
        for pattern in (
            "/sys/class/thermal/thermal_zone*/temp",
            "/sys/class/hwmon/hwmon*/temp*_input",
        )
        for path in glob.glob(pattern)
    }
    return sorted(candidates)


class ThermalSampler:
    def __init__(self) -> None:
        self.paths = temperature_paths()
        self.peaks: dict[str, float] = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            for path in self.paths:
                try:
                    raw = float(path.read_text(encoding="utf-8").strip())
                    value = raw / 1000 if raw > 1000 else raw
                    if 0 < value < 150:
                        self.peaks[str(path)] = max(self.peaks.get(str(path), value), value)
                except (OSError, ValueError):
                    continue
            self.stop.wait(0.5)

    def __enter__(self) -> "ThermalSampler":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join()


def command_output(command: list[str], fallback: str = "unknown") -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return fallback


class DiagnosticProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        self.ready = threading.Event()
        self.lines: list[str] = []
        self.thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.thread.start()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.lines.append(line)
            if line.rstrip("\n") == "cascade: ready":
                self.ready.set()

    def stderr_text(self) -> str:
        self.thread.join(timeout=2)
        return "".join(self.lines)


def loopback_capture_command(
    source_name: str, audit: Path, capture_seconds: float,
    acoustic_shift_ms: int, acoustic_tail_ms: int,
) -> list[str]:
    return [
        str(ROOT / "scripts/cascade"), "live",
        "--source", source_name,
        "--capture-seconds", f"{capture_seconds:.3f}",
        "--jsonl", "--audit", str(audit),
        "--acoustic-shift-ms", str(acoustic_shift_ms),
        "--acoustic-tail-ms", str(acoustic_tail_ms),
    ]


def run_file_transport(command: list[str]) -> tuple[int, str, dict]:
    with tempfile.TemporaryFile("w+", encoding="utf-8") as stderr:
        completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr)
        stderr.seek(0)
        stderr_text = stderr.read()
    return completed.returncode, stderr_text, {
        "name": "file",
        "command": command,
        "capture": {"status": "not-applicable", "returncode": None},
        "playback": {"status": "not-applicable", "returncode": None},
    }


def run_pipewire_transport(
    audio: Path, fixture_info: dict, audit: Path, acoustic_shift_ms: int,
    acoustic_tail_ms: int,
) -> tuple[int, str, dict]:
    loopback = PipeWireLoopback(
        pw_loopback_bin=os.environ.get("PW_LOOPBACK_BIN", "pw-loopback"),
        pw_play_bin=os.environ.get("PW_PLAY_BIN", "pw-play"),
        pw_dump_bin=os.environ.get("PW_DUMP_BIN", "pw-dump"),
    )
    capture_seconds = float(fixture_info["duration_seconds"]) + 3.0
    diagnostics: DiagnosticProcess | None = None
    failure: str | None = None
    details = {
        "name": "pipewire-loopback",
        "pipewire_version": loopback.version(),
        "virtual_node_names": {
            "prefix": loopback.prefix,
            "sink": loopback.sink_name,
            "source": loopback.source_name,
        },
        "virtual_nodes": {},
        "graph_isolation_checks": loopback.graph_checks,
        "capture": {
            "status": "not-started", "returncode": None,
            "source": loopback.source_name,
            "seconds": capture_seconds,
            "ready_seen": False,
        },
        "playback": loopback.playback_status,
        "cleanup": {"status": "pending", "virtual_nodes_absent": False},
    }
    try:
        details["virtual_nodes"] = loopback.start()
        command = loopback_capture_command(
            loopback.source_name, audit, capture_seconds,
            acoustic_shift_ms, acoustic_tail_ms,
        )
        details["capture"]["command"] = command
        diagnostics = DiagnosticProcess(command)
        ready_deadline = time.monotonic() + 200
        while not diagnostics.ready.wait(0.05):
            if diagnostics.process.poll() is not None:
                raise PipeWireError(
                    f"cascade exited before readiness with status {diagnostics.process.returncode}"
                )
            if time.monotonic() >= ready_deadline:
                raise PipeWireError("cascade readiness diagnostic timed out")
        details["capture"].update({"status": "running", "ready_seen": True})
        loopback.check_graph("capture-active", require_capture=True, timeout=5)
        loopback.start_playback(audio)
        details["playback"] = loopback.playback_status
        loopback.check_graph(
            "playback-and-capture-active", require_capture=True,
            require_playback=True, timeout=5,
        )
        loopback.wait_playback(float(fixture_info["duration_seconds"]) + 10)
        details["playback"] = loopback.playback_status
        loopback.check_graph("post-playback", require_capture=True, timeout=2)
        try:
            capture_returncode = diagnostics.process.wait(timeout=capture_seconds + 10)
        except subprocess.TimeoutExpired as error:
            raise PipeWireError("bounded live capture did not complete") from error
        details["capture"].update({
            "status": "complete" if capture_returncode == 0 else "failed",
            "returncode": capture_returncode,
        })
        if capture_returncode:
            raise PipeWireError(f"live capture failed with status {capture_returncode}")
    except (OSError, PipeWireError, subprocess.SubprocessError) as error:
        failure = str(error)
    finally:
        if diagnostics is not None and diagnostics.process.poll() is None:
            terminate_owned_process(diagnostics.process)
            details["capture"].update({
                "status": "terminated",
                "returncode": diagnostics.process.returncode,
            })
        details["cleanup"] = loopback.close()
        details["graph_isolation_checks"] = loopback.graph_checks
        details["playback"] = loopback.playback_status
    stderr_text = diagnostics.stderr_text() if diagnostics is not None else ""
    if failure:
        details["failure"] = failure
        return 1, stderr_text, details
    if details["cleanup"].get("status") != "complete":
        details["failure"] = "virtual-node cleanup failed"
        return 1, stderr_text, details
    return 0, stderr_text, details


def analyze(
    events: list[dict], result: dict, rows: list[dict], fixture_info: dict, paced: bool,
    thermal: ThermalSampler, swap_start: int, swap_end: int,
    transport: str = "file", transport_info: dict | None = None,
) -> dict:
    contiguous = [event.get("sequence") for event in events] == list(range(len(events)))
    nemotron_finals = {
        int(event["segment_id"]): event
        for event in events
        if event.get("state") == "model_final"
        and event.get("model") == "nemo:nemotron-streaming-en"
    }
    parakeet_finals = {
        int(event["segment_id"]): event
        for event in events
        if event.get("state") == "model_final"
        and event.get("model") == "nemo:parakeet-tdt-v3"
    }
    commits = sorted(
        (event for event in events if event.get("state") == "committed"),
        key=lambda event: int(event["segment_id"]),
    )
    segment_ids = [int(event["segment_id"]) for event in commits]
    commit_order = segment_ids == list(range(len(commits)))
    reference = " ".join(row["reference"] for row in rows)
    nemotron_text = " ".join(
        nemotron_finals[index]["text"] for index in sorted(nemotron_finals)
    )
    committed_text = " ".join(event["text"] for event in commits)
    nemotron_counts = errors(reference, nemotron_text)
    committed_counts = errors(reference, committed_text)
    churn_counts = errors(nemotron_text, committed_text)
    nemotron_wer = (
        nemotron_counts["errors"] / nemotron_counts["reference_words"]
        if nemotron_counts["reference_words"] else None
    )
    committed_wer = (
        committed_counts["errors"] / committed_counts["reference_words"]
        if committed_counts["reference_words"] else None
    )
    corrected = sum(
        normalize(event["text"]) != normalize(nemotron_finals[int(event["segment_id"])]["text"])
        for event in commits
        if int(event["segment_id"]) in nemotron_finals
        and event.get("model") == "nemo:parakeet-tdt-v3"
    )
    correction_lags = [
        float(event["monotonic_ms"]) - float(nemotron_finals[index]["monotonic_ms"])
        for index, event in parakeet_finals.items()
        if index in nemotron_finals
    ]
    partial_lags = [
        float(event["latency_ms"])
        for event in events
        if event.get("state") == "provisional" and event.get("latency_ms") is not None
    ]
    degraded = [event for event in commits if event.get("degraded")]
    correction_max = max(correction_lags, default=None)
    partial_p95 = percentile(partial_lags, 0.95)
    real_time_factor = result.get("real_time_factor")
    nominal_gates = {
        "contiguous_events": contiguous,
        "ordered_commits": commit_order,
        "zero_degraded_segments": len(degraded) == 0,
        "p95_partial_lag_at_most_750_ms": partial_p95 is not None and partial_p95 <= 750,
        "all_corrections_within_2500_ms": correction_max is not None and correction_max <= 2500,
        "committed_wer_not_worse": (
            nemotron_wer is not None and committed_wer is not None and committed_wer <= nemotron_wer
        ),
    }
    if transport == "pipewire-loopback":
        transport_info = transport_info or {}
        graph_checks = transport_info.get("graph_isolation_checks", [])
        nominal_gates.update({
            "one_load_per_model": (
                result.get("nemotron_loads") == 1 and result.get("parakeet_loads") == 1
            ),
            "no_swap_growth": swap_end <= swap_start,
            "real_time_factor_between_0_98_and_1_10": (
                isinstance(real_time_factor, (int, float))
                and 0.98 <= real_time_factor <= 1.10
            ),
            "physical_device_links_absent": (
                len(graph_checks) >= 3
                and all(
                    check.get("passed")
                    and not check.get("physical_or_unknown_links")
                    for check in graph_checks
                )
            ),
            "playback_complete": (
                transport_info.get("playback", {}).get("status") == "complete"
                and transport_info.get("playback", {}).get("returncode") == 0
            ),
            "capture_complete": (
                transport_info.get("capture", {}).get("status") == "complete"
                and transport_info.get("capture", {}).get("returncode") == 0
            ),
            "virtual_cleanup_complete": (
                transport_info.get("cleanup", {}).get("status") == "complete"
                and transport_info.get("cleanup", {}).get("virtual_nodes_absent") is True
            ),
        })
    return {
        "schema_version": 1,
        "status": "complete",
        "dataset": fixture_info["dataset"],
        "utterances": len(rows),
        "paced": paced,
        "transport": transport,
        "fixture": fixture_info,
        "normalization": NORMALIZATION_VERSION,
        "events": len(events),
        "segments": len(commits),
        "contiguous_events": contiguous,
        "ordered_commits": commit_order,
        "nemotron_wer": nemotron_wer,
        "nemotron_wer_counts": nemotron_counts,
        "committed_wer": committed_wer,
        "committed_wer_counts": committed_counts,
        "correction_rate": corrected / len(commits) if commits else None,
        "corrected_segments": corrected,
        "transcript_churn": (
            churn_counts["errors"] / churn_counts["reference_words"]
            if churn_counts["reference_words"] else None
        ),
        "transcript_churn_counts": churn_counts,
        "degradation_count": len(degraded),
        "degradation_reasons": [event.get("degradation_reason") for event in degraded],
        "partial_events": len(partial_lags),
        "partial_lag_p50_ms": percentile(partial_lags, 0.50),
        "partial_lag_p95_ms": partial_p95,
        "correction_lag_p50_ms": percentile(correction_lags, 0.50),
        "correction_lag_p95_ms": percentile(correction_lags, 0.95),
        "correction_lag_max_ms": correction_max,
        "real_time_factor": real_time_factor,
        "wall_ms": result.get("wall_ms"),
        "user_seconds": result.get("user_seconds"),
        "system_seconds": result.get("system_seconds"),
        "peak_rss_kb": result.get("peak_rss_kb"),
        "nemotron_loads": result.get("nemotron_loads"),
        "parakeet_loads": result.get("parakeet_loads"),
        "swap_used_start_kb": swap_start,
        "swap_used_end_kb": swap_end,
        "swap_growth_kb": max(0, swap_end - swap_start),
        "thermal_peak_c": max(thermal.peaks.values(), default=None),
        "thermal_sensors_peak_c": thermal.peaks,
        "acceptance_gates": nominal_gates if paced else None,
        "acceptance_passed": all(nominal_gates.values()) if paced else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts/cascade-benchmark")
    parser.add_argument("dataset", choices=("librispeech-test-clean", "librispeech-test-other"))
    parser.add_argument(
        "--transport", choices=("file", "pipewire-loopback"), default="file",
        help="file replays directly (default); pipewire-loopback exercises exact live capture",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--silence-ms", type=int, default=1000)
    parser.add_argument("--boundary-silence-ms", type=int, default=1000)
    parser.add_argument(
        "--selection", choices=("phrase", "hash"), default="phrase",
        help="phrase sorts by distance from three seconds for the interactive acceptance fixture; "
             "hash retains the broader long-form stress sample",
    )
    parser.add_argument("--acoustic-shift-ms", type=int, default=880)
    parser.add_argument("--acoustic-tail-ms", type=int, default=320)
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument("--paced", dest="paced", action="store_true", default=True)
    timing.add_argument("--unpaced", dest="paced", action="store_false")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        args.limit <= 0 or args.silence_ms < 1000 or args.boundary_silence_ms < 1000 or
        args.acoustic_shift_ms < 0 or args.acoustic_tail_ms < 0
    ):
        parser.error(
            "--limit must be positive, silence values must be at least 1000, "
            "and acoustic context must be nonnegative"
        )
    if args.transport == "pipewire-loopback" and not args.paced:
        parser.error("PipeWire loopback transport is inherently paced; --unpaced is invalid")

    manifest, rows = selected_rows(args.dataset, args.limit, args.selection)
    audio, fixture_info = fixture(
        args.dataset, manifest, rows, args.silence_ms, args.selection,
        args.boundary_silence_ms,
    )
    created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    mode = "paced" if args.paced else "unpaced"
    transport_label = args.transport.replace("pipewire-", "")
    base = args.output or BENCHMARKS / "cascade" / (
        f"{created}-{args.dataset}-{args.limit}-{transport_label}-{mode}"
    )
    base = base.resolve()
    audit = Path(str(base) + ".audit")
    summary_path = Path(str(base) + ".summary.json")
    if audit.exists() or summary_path.exists():
        raise SystemExit(f"error: output already exists for base: {base}")

    base.parent.mkdir(parents=True, exist_ok=True)
    swap_start = swap_used_kb()
    with ThermalSampler() as thermal:
        if args.transport == "file":
            command = [
                str(ROOT / "scripts/cascade"), "file", str(audio),
                "--paced" if args.paced else "--unpaced", "--jsonl", "--audit", str(audit),
                "--acoustic-shift-ms", str(args.acoustic_shift_ms),
                "--acoustic-tail-ms", str(args.acoustic_tail_ms),
            ]
            returncode, stderr_text, transport_info = run_file_transport(command)
        else:
            returncode, stderr_text, transport_info = run_pipewire_transport(
                audio, fixture_info, audit, args.acoustic_shift_ms,
                args.acoustic_tail_ms,
            )
    swap_end = swap_used_kb()

    common = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audit_path": str(audit),
        "summary_path": str(summary_path),
        "git_revision": command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "dirty_tree": bool(command_output(["git", "-C", str(ROOT), "status", "--porcelain"], "")),
        "image_id": command_output(
            ["docker", "image", "inspect", "asr-nemo-speech", "--format", "{{.Id}}"]
        ),
        "cpu": command_output(["lscpu", "--parse=MODELNAME"]).splitlines()[-1],
        "logical_cpus": os.cpu_count(),
        "memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
        "kernel": platform.release(),
        "docker_version": command_output(["docker", "--version"]),
        "acoustic_shift_ms": args.acoustic_shift_ms,
        "acoustic_tail_ms": args.acoustic_tail_ms,
        "selection": args.selection,
        "transport": args.transport,
        "transport_details": transport_info,
    }
    if returncode:
        failure_summary = {
            "schema_version": 1,
            "status": "failed",
            "dataset": args.dataset,
            "utterances": len(rows),
            "paced": args.paced,
            "fixture": fixture_info,
            "failure": transport_info.get("failure", "cascade transport failed"),
            "stderr_tail": stderr_text[-4000:],
            "swap_used_start_kb": swap_start,
            "swap_used_end_kb": swap_end,
            "thermal_peak_c": max(thermal.peaks.values(), default=None),
            **common,
        }
        atomic_json(summary_path, failure_summary)
        print(json.dumps(failure_summary, sort_keys=True, indent=2))
        raise SystemExit(1)

    events = [
        json.loads(line)
        for line in (audit / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    result = json.loads((audit / "result.json").read_text(encoding="utf-8"))
    summary = analyze(
        events, result, rows, fixture_info, args.paced, thermal, swap_start, swap_end,
        args.transport, transport_info,
    )
    summary.update(common)
    atomic_json(summary_path, summary)
    print(json.dumps(summary, sort_keys=True, indent=2))
    if args.paced and not summary["acceptance_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
