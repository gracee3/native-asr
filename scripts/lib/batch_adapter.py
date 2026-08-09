#!/usr/bin/env python3
"""One-container, one-model-load adapters for prepared utterance sets."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
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
    stderr: str


def _json_text(payload: dict) -> str:
    if isinstance(payload.get("text"), str):
        return payload["text"]
    transcription = payload.get("transcription", [])
    return " ".join(item.get("text", "") for item in transcription).strip()


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
        command = {
            "sherpa-onnx": lambda: _sherpa(record, files, threads, base),
            "nemo-speech": lambda: _nemo(record, base),
            "moonshine": lambda: _moonshine(record, files, base),
            "whisper-cpp": lambda: _whisper(record, files, threads, base),
        }[record["runtime"]]()
        started = time.monotonic()
        process = subprocess.run(command, text=True, capture_output=True)
        wall = time.monotonic() - started
        user_seconds, system_seconds, peak_rss_kb = _timing(process.stderr)
        hypotheses: dict[str, str] = {}
        if record["runtime"] in ("sherpa-onnx", "moonshine"):
            payloads = []
            payload_source = (process.stderr if record["alias"] == "sherpa:nemotron-streaming-en"
                              else process.stdout)
            for line in payload_source.splitlines():
                try:
                    payloads.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            for file, payload in zip(files, payloads, strict=False):
                hypotheses[file.stem] = _json_text(payload)
        else:
            for output in output_dir.rglob("*.json"):
                hypotheses[output.stem] = _json_text(json.loads(output.read_text(encoding="utf-8")))
        failures = {}
        for file in files:
            if process.returncode != 0 or file.stem not in hypotheses:
                failures[file.stem] = (process.stderr[-4000:] or
                                       f"batch runtime exited {process.returncode}")
        return BatchResult(hypotheses, failures, wall, user_seconds, system_seconds,
                           peak_rss_kb, process.stderr[-16000:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
