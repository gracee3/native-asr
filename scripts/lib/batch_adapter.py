#!/usr/bin/env python3
"""Bounded native-runtime batch adapters for prepared utterance sets."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


@dataclass
class BatchResult:
    hypotheses: dict[str, str]
    failures: dict[str, str]
    wall_seconds: float
    user_seconds: float | None
    system_seconds: float | None
    peak_rss_kb: int | None
    model_loads: int
    stderr: str


SHERPA_PARAKEET_MAX_FILES = 16
SHERPA_PARAKEET_MAX_AUDIO_SECONDS = 90.0
SHERPA_PARAKEET_LONG_AUDIO_SECONDS = 20.0


def batch_policy(record: dict[str, str]) -> str:
    if record["alias"] == "sherpa:parakeet-unified-en":
        return (f"bounded-{SHERPA_PARAKEET_MAX_FILES}-files-"
                f"{SHERPA_PARAKEET_MAX_AUDIO_SECONDS:g}-audio-seconds-"
                f"vad-over-{SHERPA_PARAKEET_LONG_AUDIO_SECONDS:g}-seconds-v2")
    if record["alias"] == "nemo:parakeet-ctc-1.1b":
        return "runtime-native-json-minus-nan-confidence-null-v2"
    return "runtime-native-v1"


def _json_text(payload: dict) -> str:
    if isinstance(payload.get("text"), str):
        return payload["text"]
    transcription = payload.get("transcription", [])
    return " ".join(item.get("text", "") for item in transcription).strip()


def _runtime_json_payload(path: Path) -> dict:
    """Load runtime JSON, canonicalizing its known non-finite CTC confidence."""
    source = path.read_text(encoding="utf-8")
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        # NeMo-Speech.cpp can serialize an undefined aggregate CTC confidence
        # as the non-standard JSON number -nan. Replace only an unquoted value
        # token; transcript strings containing the same characters are kept.
        normalized: list[str] = []
        index = 0
        in_string = False
        escaped = False
        replacements = 0
        value_prefix = " \t\r\n:[,"
        value_suffix = " \t\r\n,]}"
        while index < len(source):
            character = source[index]
            if in_string:
                normalized.append(character)
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                index += 1
                continue
            if character == '"':
                in_string = True
                normalized.append(character)
                index += 1
                continue
            if (source.startswith("-nan", index)
                    and (index == 0 or source[index - 1] in value_prefix)
                    and (index + 4 == len(source) or source[index + 4] in value_suffix)):
                normalized.append("null")
                replacements += 1
                index += 4
                continue
            normalized.append(character)
            index += 1
        if not replacements:
            raise
        return json.loads("".join(normalized))


def _container_base(runtime: str, models: Path, input_dir: Path, output_dir: Path) -> list[str]:
    image = {"sherpa-onnx": "asr-sherpa-onnx", "nemo-speech": "asr-nemo-speech",
             "moonshine": "asr-moonshine", "whisper-cpp": "asr-whisper-cpp"}[runtime]
    user = "65532:65532" if os.getuid() == 0 else f"{os.getuid()}:{os.getgid()}"
    return [
        os.environ.get("NATIVE_ASR_CONTAINER_ENGINE", "docker"), "run", "--rm", "--network", "none",
        "--user", user,
        "--mount", f"type=bind,source={(models / runtime).resolve()},target=/models,readonly",
        "--mount", f"type=bind,source={input_dir.resolve()},target=/audio,readonly",
        "--mount", f"type=bind,source={output_dir.resolve()},target=/output",
        image,
    ]


def _timed(base: list[str], executable: str, arguments: list[str]) -> list[str]:
    return base[:-1] + [
        "--entrypoint", "/usr/bin/time", base[-1], "-f",
        "NATIVE_ASR_TIME\\t%U\\t%S\\t%M\\t%x", executable, *arguments,
    ]


def _sherpa(record: dict[str, str], files: list[Path], threads: int, base: list[str]) -> list[str]:
    destination = record["destination"].split("/", 1)[1]
    model = f"/models/{destination}"
    common = [f"--num-threads={threads}"]
    alias = record["alias"]
    if alias == "sherpa:parakeet-unified-en":
        binary = "/opt/native-asr/bin/sherpa-onnx-offline"
        common += [f"--encoder={model}/encoder.int8.onnx", f"--decoder={model}/decoder.int8.onnx",
                   f"--joiner={model}/joiner.int8.onnx", f"--tokens={model}/tokens.txt",
                   "--model-type=nemo_transducer"]
    elif alias == "sherpa:canary-180m-flash":
        binary = "/opt/native-asr/bin/sherpa-onnx-offline"
        common += [f"--canary-encoder={model}/encoder.int8.onnx",
                   f"--canary-decoder={model}/decoder.int8.onnx", f"--tokens={model}/tokens.txt",
                   "--canary-src-lang=en", "--canary-tgt-lang=en"]
    else:
        binary = "/opt/native-asr/bin/sherpa-onnx"
        common += [f"--encoder={model}/encoder.int8.onnx", f"--decoder={model}/decoder.int8.onnx",
                   f"--joiner={model}/joiner.int8.onnx", f"--tokens={model}/tokens.txt"]
    return _timed(base, binary, [*common, *(f"/audio/{path.name}" for path in files)])


def _sherpa_parakeet_vad(record: dict[str, str], file: Path, threads: int,
                         base: list[str]) -> list[str]:
    destination = record["destination"].split("/", 1)[1]
    model = f"/models/{destination}"
    return _timed(base, "/opt/native-asr/bin/sherpa-onnx-vad-with-offline-asr", [
        "--silero-vad-model=/models/_shared/silero_vad.onnx",
        "--silero-vad-threshold=0.2", "--silero-vad-min-speech-duration=0.2",
        f"--num-threads={threads}", f"--encoder={model}/encoder.int8.onnx",
        f"--decoder={model}/decoder.int8.onnx", f"--joiner={model}/joiner.int8.onnx",
        f"--tokens={model}/tokens.txt", "--model-type=nemo_transducer",
        f"/audio/{file.name}",
    ])


def _nemo(record: dict[str, str], base: list[str]) -> list[str]:
    model = "/models/" + record["destination"].split("/", 1)[1]
    arguments = ["transcribe", "/audio", "--model", model, "--device", "cpu",
                 "--format", "json", "--output-dir", "/output", "--word-times",
                 "--concurrency", "1", "--force"]
    if "nemotron" in record["alias"]:
        arguments.append("--stream")
    return _timed(base, "/opt/native-asr/bin/nemo-speech", arguments)


def _moonshine(record: dict[str, str], files: list[Path], base: list[str]) -> list[str]:
    model = "/models/" + record["destination"].split("/", 1)[1]
    return _timed(base, "/opt/moonshine/bin/native-asr-moonshine-core",
                  ["--model", model, *(f"/audio/{path.name}" for path in files)])


def _whisper(record: dict[str, str], files: list[Path], threads: int, base: list[str]) -> list[str]:
    model = "/models/" + record["destination"].split("/", 1)[1]
    args = ["-m", model, "-l", "en", "-t", str(threads), "-oj", "-np"]
    for path in files:
        args += ["-f", f"/audio/{path.name}", "-of", f"/output/{path.stem}"]
    return _timed(base, "/opt/whisper/bin/whisper-cli", args)


def _timing(stderr: str) -> tuple[float | None, float | None, int | None]:
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


def _sherpa_vad_text(output: str) -> str:
    segments = []
    for line in output.splitlines():
        match = re.match(r"^\s*\d+(?:\.\d+)?\s+--\s+\d+(?:\.\d+)?:\s*(.*)$", line)
        if match and match.group(1).strip():
            segments.append(match.group(1).strip())
    return " ".join(segments)


def _sherpa_parakeet_groups(files: list[Path], utterances: list[dict]) \
        -> list[tuple[list[Path], bool]]:
    groups: list[tuple[list[Path], bool]] = []
    current: list[Path] = []
    current_audio_seconds = 0.0
    for file, utterance in zip(files, utterances, strict=True):
        duration = float(utterance["duration_seconds"])
        if duration > SHERPA_PARAKEET_LONG_AUDIO_SECONDS:
            if current:
                groups.append((current, False))
                current = []
                current_audio_seconds = 0.0
            groups.append(([file], True))
            continue
        if current and (len(current) >= SHERPA_PARAKEET_MAX_FILES or
                        current_audio_seconds + duration > SHERPA_PARAKEET_MAX_AUDIO_SECONDS):
            groups.append((current, False))
            current = []
            current_audio_seconds = 0.0
        current.append(file)
        current_audio_seconds += duration
    if current:
        groups.append((current, False))
    return groups


def run_batch(record: dict[str, str], utterances: list[dict], models: Path,
              cache: Path, threads: int) -> BatchResult:
    batch_root = cache / "bench-batch"
    batch_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run.", dir=batch_root))
    input_dir, output_dir = work / "input", work / "output"
    input_dir.mkdir(); output_dir.mkdir()
    files = []
    try:
        for row in utterances:
            target = input_dir / f"{row['utterance_id']}.wav"
            try:
                os.link(row["prepared_path"], target)
            except OSError:
                shutil.copy2(row["prepared_path"], target)
            files.append(target)
        base = _container_base(record["runtime"], models, input_dir, output_dir)
        groups = ([(files, False)] if record["alias"] != "sherpa:parakeet-unified-en" else
                  _sherpa_parakeet_groups(files, utterances))
        commands = []
        for group, use_vad in groups:
            if use_vad:
                commands.append(_sherpa_parakeet_vad(record, group[0], threads, base))
            else:
                commands.append({
                    "sherpa-onnx": lambda: _sherpa(record, group, threads, base),
                    "nemo-speech": lambda: _nemo(record, base),
                    "moonshine": lambda: _moonshine(record, group, base),
                    "whisper-cpp": lambda: _whisper(record, group, threads, base),
                }[record["runtime"]]())
        started = time.monotonic()
        processes = [subprocess.run(command, text=True, capture_output=True)
                     for command in commands]
        wall = time.monotonic() - started
        hypotheses: dict[str, str] = {}
        failures: dict[str, str] = {}
        timing = [_timing(process.stderr) for process in processes]
        user_seconds = (sum(row[0] for row in timing if row[0] is not None)
                        if all(row[0] is not None for row in timing) else None)
        system_seconds = (sum(row[1] for row in timing if row[1] is not None)
                          if all(row[1] is not None for row in timing) else None)
        peaks = [row[2] for row in timing if row[2] is not None]
        peak_rss_kb = max(peaks) if len(peaks) == len(timing) else None
        for (group, use_vad), process in zip(groups, processes, strict=True):
            if use_vad:
                hypotheses[group[0].stem] = _sherpa_vad_text(process.stdout)
            elif record["runtime"] in ("sherpa-onnx", "moonshine"):
                payloads = []
                payload_source = (process.stderr
                                  if record["alias"] == "sherpa:nemotron-streaming-en"
                                  else process.stdout)
                for line in payload_source.splitlines():
                    try:
                        payloads.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                for file, payload in zip(group, payloads, strict=False):
                    hypotheses[file.stem] = _json_text(payload)
            # Sherpa and Moonshine emit hypotheses on their captured streams,
            # so they can be validated here. NeMo and Whisper write JSON files
            # under /output; validate those only after loading that directory
            # below.
            if record["runtime"] in ("sherpa-onnx", "moonshine"):
                for file in group:
                    if process.returncode != 0 or file.stem not in hypotheses:
                        failures[file.stem] = (process.stderr[-4000:] or
                                               f"batch runtime exited {process.returncode}")
            if (record["alias"] == "sherpa:parakeet-unified-en" and group and
                    all(file.stem in hypotheses and not hypotheses[file.stem].strip()
                        for file in group)):
                message = ("sherpa parakeet batch returned only empty hypotheses "
                           f"for {len(group)} inputs under policy {batch_policy(record)}")
                failures.update({file.stem: message for file in group})
        if record["runtime"] not in ("sherpa-onnx", "moonshine"):
            for output in output_dir.rglob("*.json"):
                hypotheses[output.stem] = _json_text(_runtime_json_payload(output))
            process = processes[0]
            for file in files:
                if process.returncode != 0 or file.stem not in hypotheses:
                    failures[file.stem] = (process.stderr[-4000:] or
                                           f"batch runtime exited {process.returncode}")
        stderr = "\n".join(process.stderr for process in processes)
        return BatchResult(hypotheses, failures, wall, user_seconds, system_seconds,
                           peak_rss_kb, len(processes), stderr[-16000:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
