#!/usr/bin/env python3
"""Host orchestration and event validation for the experimental two-pass cascade."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, TextIO
import uuid


NEMOTRON_ALIAS = "nemo:nemotron-streaming-en"
PARAKEET_ALIAS = "nemo:parakeet-tdt-v3"
MODEL_ALIASES = (NEMOTRON_ALIAS, PARAKEET_ALIAS)
IMAGE = "asr-nemo-speech"
ENDPOINT_SILENCE_MILLISECONDS = 1_200
ENDPOINT_POLICY = {
    "kind": "token_silence",
    "silence_milliseconds": ENDPOINT_SILENCE_MILLISECONDS,
}
TIME_RE = re.compile(
    r"^NATIVE_ASR_TIME\t([^\t]+)\t([^\t]+)\t([^\t]+)\t([^\t]+)$", re.MULTILINE
)
EVENT_TYPES = {
    "session_started", "transcript_update", "session_warning", "session_metrics",
    "session_completed", "session_error", "session_cancelled",
}
TERMINAL_TYPES = {"session_completed", "session_error", "session_cancelled"}


class ValidationError(RuntimeError):
    """A command or preflight error before the measured job starts."""


class JobFailure(RuntimeError):
    """A failure after the audit staging boundary exists."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        os.chmod(path, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Any) -> None:
    _write(path, _json_bytes(value))


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
            return
        error = ctypes.get_errno()
        if error != errno.ENOSYS:
            raise OSError(error, os.strerror(error), destination)
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination)
    os.rename(source, destination)


def _manifest(path: Path) -> dict[str, dict[str, str]]:
    fields = ("artifact_id", "alias", "runtime", "name", "source", "revision", "filename",
              "destination", "sha256", "license", "packaging", "requires", "notes")
    records: dict[str, dict[str, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"model manifest is not readable: {path}: {error}") from error
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        values = line.split("|")
        if len(values) != len(fields):
            raise ValidationError(f"invalid model manifest record at line {number}")
        record = dict(zip(fields, values))
        records[record["alias"]] = record
    return records


def _command_output(command: list[str], description: str, env: dict[str, str]) -> str:
    process = subprocess.run(command, text=True, capture_output=True, env=env)
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
        raise ValidationError(f"{description} failed: {detail}")
    return process.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _adapter_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(paths[0].parents[1]).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def assemble_transcript(segments: list[dict[str, Any]]) -> str:
    """Join authoritative nonempty segments without lexical rewriting."""
    return " ".join(
        segment["authoritative"]["text"].strip()
        for segment in segments
        if segment.get("authoritative", {}).get("text", "").strip()
    )


class EventValidator:
    """Validate event schema 1 and retain exact finalized segment state."""

    def __init__(self, session_id: str, expected_pace: bool | None = None):
        self.session_id = session_id
        self.expected_pace = expected_pace
        self.next_sequence = 1
        self.last_emission = 0.0
        self.last_audio_position = 0.0
        self.started = False
        self.metrics: dict[str, Any] | None = None
        self.terminal: dict[str, Any] | None = None
        self.segments: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.warnings = 0

    @staticmethod
    def _words(event: dict[str, Any], start: float, end: float,
               enforce_segment_bounds: bool = True) -> None:
        words = event.get("words")
        if words is None:
            return
        if not isinstance(words, list):
            raise ValueError("transcript words must be an array")
        # Nemotron's native EOS flush may place the first absolute word just
        # before the endpointed segment boundary. When bounds are disabled,
        # timestamp ordering starts at zero rather than at that boundary.
        prior = start if enforce_segment_bounds else 0.0
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("word"), str):
                raise ValueError("transcript word must carry text")
            word_start = _finite_number(word.get("start_seconds"), "word start")
            word_end = _finite_number(word.get("end_seconds"), "word end")
            if word_end < word_start or word_start < 0:
                raise ValueError("word timestamp is invalid")
            if (enforce_segment_bounds and
                    (word_start + 0.002 < start or word_end > end + 0.002)):
                raise ValueError("word timestamp falls outside the source segment")
            if word_start + 0.002 < prior:
                raise ValueError("word timestamps are not monotonic")
            prior = word_start

    def _common(self, event: dict[str, Any]) -> None:
        if event.get("schema_version") != 1:
            raise ValueError("unsupported cascade event schema")
        if event.get("session_id") != self.session_id:
            raise ValueError("cascade event session_id changed")
        if event.get("sequence") != self.next_sequence:
            raise ValueError(
                f"cascade event sequence is not contiguous: expected {self.next_sequence}"
            )
        if event.get("event") not in EVENT_TYPES:
            raise ValueError("unknown cascade event type")
        emission = _finite_number(event.get("emitted_monotonic_seconds"), "emission time")
        audio = _finite_number(event.get("audio_position_seconds"), "audio position")
        if emission < 0 or audio < 0:
            raise ValueError("cascade event clocks must be nonnegative")
        if emission < self.last_emission:
            raise ValueError("cascade emission time moved backwards")
        if audio + 0.002 < self.last_audio_position:
            raise ValueError("cascade audio position moved backwards")
        if self.terminal is not None:
            raise ValueError("cascade event followed a terminal event")
        self.last_audio_position = max(self.last_audio_position, audio)
        self.last_emission = emission

    def _transcript(self, event: dict[str, Any]) -> None:
        required_strings = ("segment_id", "track_id", "state", "text")
        if any(not isinstance(event.get(key), str) for key in required_strings):
            raise ValueError("transcript update is missing a string field")
        segment_id = event["segment_id"]
        expected_id = f"segment-{len(self.segments) + 1:06d}"
        if segment_id != expected_id:
            raise ValueError(f"expected segment_id {expected_id}")
        source = event.get("source_time")
        if not isinstance(source, dict):
            raise ValueError("transcript update has no source_time")
        start = _finite_number(source.get("start_seconds"), "segment start")
        end = _finite_number(source.get("end_seconds"), "segment end")
        expected_start = self.segments[-1]["source_time"]["end_seconds"] if self.segments else 0.0
        if abs(start - expected_start) > 0.002 or end + 0.002 < start:
            raise ValueError("segment source ranges are not contiguous")
        if end > float(event["audio_position_seconds"]) + 0.002:
            raise ValueError("segment range leads the event audio position")
        revision = event.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("transcript revision must be a positive integer")
        if self.current is None:
            self.current = {
                "segment_id": segment_id,
                "source_time": {"start_seconds": start, "end_seconds": end},
                "nemotron_updates": [],
                "nemotron_final": None,
                "authoritative": None,
            }
        elif self.current["segment_id"] != segment_id:
            raise ValueError("new segment began before the prior cascade final")

        track, state = event["track_id"], event["state"]
        if track == "nemotron":
            if state not in {"provisional", "segment_final"}:
                raise ValueError("invalid Nemotron transcript state")
            updates = self.current["nemotron_updates"]
            if revision != len(updates) + 1 or self.current["nemotron_final"] is not None:
                raise ValueError("Nemotron revisions are not contiguous")
            if event.get("model_alias") != NEMOTRON_ALIAS:
                raise ValueError("Nemotron update has the wrong model alias")
            if state == "provisional":
                if not event["text"].strip():
                    raise ValueError("empty provisional update")
                previous = next(
                    (item for item in reversed(updates) if item["state"] == "provisional"), None
                )
                if previous is not None and previous["text"] == event["text"]:
                    raise ValueError("identical consecutive provisional update")
            else:
                # Upstream's Nemotron EOS flush can assign terminal words a
                # tail beyond the exact endpoint. Preserve those absolute
                # native values rather than clipping them to the source slice.
                self._words(event, start, end, enforce_segment_bounds=False)
                self.current["nemotron_final"] = event
                self.current["source_time"] = {"start_seconds": start, "end_seconds": end}
            updates.append(event)
        elif track == "authoritative":
            if state != "cascade_final" or revision != 1:
                raise ValueError("invalid authoritative transcript state")
            nemotron = self.current["nemotron_final"]
            if nemotron is None or self.current["authoritative"] is not None:
                raise ValueError("cascade final does not follow one Nemotron final")
            if source != nemotron["source_time"]:
                raise ValueError("cascade final changed the endpointed source range")
            supersedes = event.get("supersedes")
            if supersedes != {"track_id": "nemotron", "revision": nemotron["revision"]}:
                raise ValueError("cascade final has the wrong superseded revision")
            selection = event.get("selection")
            if selection == "parakeet":
                if event.get("model_alias") != PARAKEET_ALIAS or not event["text"].strip():
                    raise ValueError("invalid Parakeet authoritative selection")
            elif selection == "nemotron_fallback":
                if (event.get("model_alias") != NEMOTRON_ALIAS or
                        event["text"] != nemotron["text"] or not event["text"].strip()):
                    raise ValueError("invalid Nemotron fallback selection")
            elif selection == "silence":
                if event.get("model_alias") is not None or event["text"].strip():
                    raise ValueError("invalid silence selection")
            else:
                raise ValueError("unknown cascade selection")
            self._words(
                event, start, end, enforce_segment_bounds=(selection == "parakeet")
            )
            self.current["authoritative"] = event
            self.segments.append(self.current)
            self.current = None
        else:
            raise ValueError("unknown transcript track")

    def accept(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise ValueError("cascade event must be a JSON object")
        self._common(event)
        kind = event["event"]
        if not self.started:
            if kind != "session_started":
                raise ValueError("first cascade event is not session_started")
            if event.get("models") != list(MODEL_ALIASES):
                raise ValueError("cascade model pair changed")
            endpoint = event.get("endpointing")
            if endpoint != ENDPOINT_POLICY:
                raise ValueError("cascade endpoint policy changed")
            if event.get("chunk_milliseconds") != 20 or not isinstance(event.get("paced"), bool):
                raise ValueError("cascade chunk or pacing policy is malformed")
            if self.expected_pace is not None and event["paced"] != self.expected_pace:
                raise ValueError("runtime pacing policy disagrees with the command")
            self.started = True
        elif kind == "session_started":
            raise ValueError("duplicate session_started event")
        elif kind == "transcript_update":
            self._transcript(event)
        elif kind == "session_warning":
            if not isinstance(event.get("code"), str) or not isinstance(event.get("message"), str):
                raise ValueError("session warning is malformed")
            self.warnings += 1
        elif kind == "session_metrics":
            if self.metrics is not None or self.current is not None:
                raise ValueError("session metrics arrived before finalized segment state")
            loads = event.get("model_load_counts")
            if loads != {NEMOTRON_ALIAS: 1, PARAKEET_ALIAS: 1}:
                raise ValueError("cascade did not report exactly one load per model")
            if not isinstance(event.get("timing"), dict) or not isinstance(event.get("counts"), dict):
                raise ValueError("session metrics are malformed")
            for name, value in event["timing"].items():
                if _finite_number(value, f"native timing {name}") < 0:
                    raise ValueError("native timings must be nonnegative")
            counts = event["counts"]
            expected_counts = {
                "segments": len(self.segments),
                "provisional_updates": sum(
                    update["state"] == "provisional"
                    for segment in self.segments for update in segment["nemotron_updates"]
                ),
                "parakeet_segments": sum(
                    segment["authoritative"]["selection"] == "parakeet"
                    for segment in self.segments
                ),
                "nemotron_fallbacks": sum(
                    segment["authoritative"]["selection"] == "nemotron_fallback"
                    for segment in self.segments
                ),
                "silence_segments": sum(
                    segment["authoritative"]["selection"] == "silence"
                    for segment in self.segments
                ),
                "warnings": self.warnings,
            }
            if any(counts.get(name) != value for name, value in expected_counts.items()):
                raise ValueError("session metric counts disagree with emitted events")
            self.metrics = event
        elif kind == "session_completed":
            if self.metrics is None or self.current is not None:
                raise ValueError("session completed without terminal metrics")
            text = assemble_transcript(self.segments)
            if event.get("text") != text or event.get("segment_count") != len(self.segments):
                raise ValueError("session completion disagrees with authoritative segments")
            self.terminal = event
        elif kind in {"session_error", "session_cancelled"}:
            if not isinstance(event.get("message"), str):
                raise ValueError("failure terminal event has no message")
            self.terminal = event
        self.next_sequence += 1

    def synthetic(self, kind: str, message: str, stage: str, elapsed: float) -> dict[str, Any]:
        event = {
            "schema_version": 1,
            "session_id": self.session_id,
            "sequence": self.next_sequence,
            "event": kind,
            "emitted_monotonic_seconds": max(0.0, elapsed, self.last_emission),
            "audio_position_seconds": self.last_audio_position,
            "stage": stage,
            "message": message,
            "origin": "host",
        }
        self.accept(event)
        return event


class CascadeJob:
    def __init__(self, root: Path, output: Path, audio: Path,
                 records: dict[str, dict[str, str]], pace: bool, env: dict[str, str]):
        self.root, self.output, self.audio = root, output, audio
        self.records, self.pace, self.env = records, pace, env
        self.engine = env.get("NATIVE_ASR_CONTAINER_ENGINE", "docker")
        self.models_root = Path(env.get("NATIVE_ASR_MODELS", "/data/models"))
        self.session_id = str(uuid.uuid4())
        self.validator = EventValidator(self.session_id, expected_pace=pace)
        self.stage: Path | None = None
        self.current: subprocess.Popen[str] | None = None
        self.cancelled = False
        self.cancel_started: float | None = None
        self.started_monotonic = 0.0
        self.created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.audio_duration = 0.0
        self.audio_sha256 = ""
        self.image_id = ""
        self.git_revision = ""
        self.git_dirty = False
        self.adapter_sha256 = ""
        self.failure: dict[str, Any] | None = None
        self.execution: dict[str, Any] | None = None
        self.invalid_line: str | None = None

    def cancel(self, _signum: int, _frame: Any) -> None:
        self.cancelled = True
        if self.cancel_started is None:
            self.cancel_started = time.monotonic()
        if self.current is not None and self.current.poll() is None:
            try:
                os.killpg(self.current.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    @staticmethod
    def _artifact(record: dict[str, str]) -> dict[str, str]:
        return {key: record[key] for key in (
            "artifact_id", "alias", "revision", "sha256", "license", "filename",
        )}

    def preflight(self) -> None:
        if os.path.lexists(self.output):
            raise ValidationError(f"output path already exists: {self.output}")
        if not self.output.parent.is_dir():
            raise ValidationError(f"output parent is not a directory: {self.output.parent}")
        if not self.audio.is_file() or not os.access(self.audio, os.R_OK):
            raise ValidationError(f"audio input is not a readable file: {self.audio}")
        for command in (self.engine, "ffprobe", "git"):
            if shutil.which(command, path=self.env.get("PATH")) is None:
                raise ValidationError(f"required command is unavailable: {command}")
        verify = self.root / "scripts/verify-models"
        if not os.access(verify, os.X_OK):
            raise ValidationError("model verification script is not executable")
        for alias in MODEL_ALIASES:
            record = self.records.get(alias)
            if record is None or record["runtime"] != "nemo-speech":
                raise ValidationError(f"fixed cascade model is unavailable: {alias}")
        verified = self.env.get("NATIVE_ASR_MODEL_VERIFIED_ALIASES", "").split(",")
        if verified != list(MODEL_ALIASES):
            _command_output([str(verify), *MODEL_ALIASES], "model verification", self.env)
        self.models_root = self.models_root.expanduser().resolve(strict=True)
        runtime_root = self.models_root / "nemo-speech"
        if not runtime_root.is_dir():
            raise ValidationError(f"NeMo model root is not a directory: {runtime_root}")
        if "," in str(runtime_root) or "," in str(self.audio.parent):
            raise ValidationError("Docker --mount paths containing commas are unsupported")
        self.image_id = _command_output(
            [self.engine, "image", "inspect", IMAGE, "--format", "{{.Id}}"],
            f"container image inspection for {IMAGE}", self.env,
        ).splitlines()[-1]
        if not self.image_id:
            raise ValidationError(f"container image inspection returned no ID: {IMAGE}")
        duration = _command_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", "--", str(self.audio),
        ], "audio duration probe", self.env)
        try:
            self.audio_duration = float(duration)
        except ValueError as error:
            raise ValidationError(f"audio duration is invalid: {duration}") from error
        if not math.isfinite(self.audio_duration) or self.audio_duration <= 0:
            raise ValidationError(f"audio duration must be positive: {duration}")
        self.audio_sha256 = _sha256(self.audio)
        self.git_revision = _command_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], "Git revision lookup", self.env
        )
        self.git_dirty = bool(_command_output(
            ["git", "-C", str(self.root), "status", "--porcelain"],
            "Git worktree lookup", self.env,
        ))
        self.adapter_sha256 = _adapter_sha256([
            self.root / "scripts/cascade", self.root / "scripts/lib/cascade.py",
            self.root / "docker/nemo-speech/entrypoint.sh",
            self.root / "docker/nemo-speech/native-asr-cascade.cpp",
            self.root / "docker/nemo-speech/Dockerfile",
        ])

    def _prepare_stage(self) -> None:
        self.stage = Path(tempfile.mkdtemp(
            prefix=f".{self.output.name}.native-asr.", dir=self.output.parent
        ))
        os.chmod(self.stage, 0o700)
        (self.stage / "logs").mkdir(mode=0o700)
        _write(self.stage / "events.jsonl", b"")
        _write(self.stage / "logs/runtime.stderr.log", b"")

    def _container_command(self) -> list[str]:
        assert self.stage is not None
        runtime_root = self.models_root / "nemo-speech"
        audio_dir, audio_name = self.audio.parent, self.audio.name
        if "," in str(runtime_root) or "," in str(audio_dir):
            raise JobFailure("launch", "Docker --mount paths containing commas are unsupported")
        container_user = "65532:65532" if os.getuid() == 0 else f"{os.getuid()}:{os.getgid()}"
        command = [
            self.engine, "run", "--rm", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,mode=1777", "--user", container_user,
            "--cidfile", str(self.stage / "container.cid"),
            "--mount", f"type=bind,source={runtime_root},target=/models,readonly",
            "--mount", f"type=bind,source={audio_dir},target=/audio,readonly",
            "--entrypoint", "/usr/bin/time", IMAGE,
            "-f", "NATIVE_ASR_TIME\t%U\t%S\t%M\t%x",
            "/usr/local/bin/native-asr-nemo", "cascade",
            "--session-id", self.session_id,
            "--stream-model", "/models/nemotron-streaming-en/nemotron-speech-streaming-en-0.6b.q8_0.gguf",
            "--final-model", "/models/parakeet-tdt-v3/parakeet-tdt-0.6b-v3.q8_0.gguf",
        ]
        if self.pace:
            command.append("--pace")
        command.append(f"/audio/{audio_name}")
        return command

    @staticmethod
    def _reader(name: str, stream: TextIO, messages: queue.Queue[tuple[str, str | None]]) -> None:
        try:
            for line in stream:
                messages.put((name, line))
        finally:
            messages.put((name, None))

    def _append_event(self, handle: TextIO, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write(line + "\n")
        handle.flush()
        print(line, flush=True)

    def _cleanup_container(self) -> None:
        if self.stage is None:
            return
        cidfile = self.stage / "container.cid"
        try:
            container_id = cidfile.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if container_id and re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
            subprocess.run(
                [self.engine, "rm", "--force", container_id], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=self.env, check=False,
            )

    def _discard_cidfile(self) -> None:
        if self.stage is not None:
            (self.stage / "container.cid").unlink(missing_ok=True)

    def _run_runtime(self) -> tuple[int, str]:
        assert self.stage is not None
        command = self._container_command()
        print("cascade: starting fixed Nemotron -> Parakeet session", file=sys.stderr, flush=True)
        self.started_monotonic = time.monotonic()
        self.current = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1, env={**self.env, "LC_ALL": "C"},
            start_new_session=True,
        )
        assert self.current.stdout is not None and self.current.stderr is not None
        messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threads = [
            threading.Thread(target=self._reader, args=("stdout", self.current.stdout, messages),
                             daemon=True),
            threading.Thread(target=self._reader, args=("stderr", self.current.stderr, messages),
                             daemon=True),
        ]
        for thread in threads:
            thread.start()
        closed: set[str] = set()
        stderr_text = ""
        event_path = self.stage / "events.jsonl"
        stderr_path = self.stage / "logs/runtime.stderr.log"
        with event_path.open("a", encoding="utf-8") as events, \
                stderr_path.open("a", encoding="utf-8") as diagnostics:
            while len(closed) < 2:
                try:
                    source, line = messages.get(timeout=0.25)
                except queue.Empty:
                    if (self.cancelled and self.cancel_started is not None and
                            self.current.poll() is None and
                            time.monotonic() - self.cancel_started > 3.0):
                        try:
                            os.killpg(self.current.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    continue
                if line is None:
                    closed.add(source)
                    continue
                if source == "stderr":
                    diagnostics.write(line)
                    diagnostics.flush()
                    stderr_text += line
                    sys.stderr.write(line)
                    sys.stderr.flush()
                    continue
                if self.invalid_line is not None:
                    diagnostics.write(f"host: ignored stdout after malformed event: {line}")
                    diagnostics.flush()
                    continue
                try:
                    event = json.loads(line)
                    self.validator.accept(event)
                except (json.JSONDecodeError, ValueError) as error:
                    self.invalid_line = line.rstrip("\n")
                    diagnostics.write(f"host: malformed cascade event: {error}: {line}")
                    diagnostics.flush()
                    continue
                self._append_event(events, event)
            events.flush()
            os.fsync(events.fileno())
            diagnostics.flush()
            os.fsync(diagnostics.fileno())
        returncode = self.current.wait()
        self.current = None
        for thread in threads:
            thread.join(timeout=1)
        if self.cancelled:
            self._cleanup_container()
        return returncode, stderr_text

    def _execution_metrics(self, returncode: int, stderr: str) -> None:
        matches = list(TIME_RE.finditer(stderr))
        wall = time.monotonic() - self.started_monotonic
        self.execution = {
            "wall_seconds": wall, "exit_status": returncode, "user_seconds": None,
            "system_seconds": None, "cpu_seconds": None, "peak_rss_kb": None,
        }
        if not matches:
            raise JobFailure("runtime_metrics", "runtime emitted no process timing metrics")
        values = matches[-1].groups()
        try:
            user, system, rss, timed_exit = float(values[0]), float(values[1]), int(values[2]), int(values[3])
        except ValueError as error:
            raise JobFailure("runtime_metrics", "runtime process timing metrics are malformed") from error
        self.execution.update({
            "user_seconds": user, "system_seconds": system, "cpu_seconds": user + system,
            "peak_rss_kb": rss,
        })
        if timed_exit != returncode:
            raise JobFailure("runtime_metrics", "runtime and timing exit statuses disagree")

    def _append_synthetic(self, kind: str, message: str, stage: str) -> None:
        assert self.stage is not None
        if self.validator.terminal is not None:
            return
        with (self.stage / "events.jsonl").open("a", encoding="utf-8") as events:
            elapsed = time.monotonic() - self.started_monotonic
            if not self.validator.started:
                started = {
                    "schema_version": 1, "session_id": self.session_id,
                    "sequence": self.validator.next_sequence, "event": "session_started",
                    "emitted_monotonic_seconds": max(0.0, elapsed),
                    "audio_position_seconds": self.validator.last_audio_position,
                    "models": list(MODEL_ALIASES), "chunk_milliseconds": 20,
                    "endpointing": ENDPOINT_POLICY,
                    "paced": self.pace, "origin": "host_recovery",
                }
                self.validator.accept(started)
                self._append_event(events, started)
            event = self.validator.synthetic(kind, message, stage, elapsed)
            self._append_event(events, event)
            events.flush()
            os.fsync(events.fileno())

    def _result(self, status: str) -> dict[str, Any]:
        metrics = self.validator.metrics or {}
        counts = metrics.get("counts") if isinstance(metrics.get("counts"), dict) else {}
        loads = metrics.get("model_load_counts") if isinstance(
            metrics.get("model_load_counts"), dict
        ) else {}
        models = []
        for alias in MODEL_ALIASES:
            models.append({
                "alias": alias,
                "role": "streaming_first_pass" if alias == NEMOTRON_ALIAS else "accurate_second_pass",
                "artifact": self._artifact(self.records[alias]),
                "load_count": loads.get(alias),
            })
        return {
            "schema_version": 1,
            "status": status,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "text": assemble_transcript(self.validator.segments) if status == "complete" else None,
            "source": {
                "path": str(self.audio), "filename": self.audio.name,
                "sha256": self.audio_sha256, "duration_seconds": self.audio_duration,
                "copied_into_bundle": False,
            },
            "models": models,
            "container": {"image": IMAGE, "image_id": self.image_id, "network": "none",
                          "user": "unprivileged", "read_only_root": True},
            "provenance": {
                "git_revision": self.git_revision, "git_dirty": self.git_dirty,
                "adapter_sha256": self.adapter_sha256,
            },
            "policy": {
                "model_pair_fixed": True, "chunk_milliseconds": 20,
                "endpointing": ENDPOINT_POLICY,
                "pacing": "real_time" if self.pace else "unpaced",
                "segment_boundary": "natural_endpoint_or_eof",
                "pass_two": "synchronous_exact_endpoint_slice",
                "empty_or_error": "explicit_nemotron_fallback_or_silence",
            },
            "timing": {"process": self.execution, "native": metrics.get("timing")},
            "counts": {
                "segments": len(self.validator.segments),
                "provisional_updates": counts.get("provisional_updates"),
                "parakeet_segments": counts.get("parakeet_segments"),
                "nemotron_fallbacks": counts.get("nemotron_fallbacks"),
                "silence_segments": counts.get("silence_segments"),
                "warnings": self.validator.warnings,
            },
            "failure": self.failure,
            "artifacts": {
                "result": "result.json", "transcript": "transcript.txt" if status == "complete" else None,
                "events": "events.jsonl", "segments": "segments.json",
                "runtime_stderr": "logs/runtime.stderr.log",
            },
        }

    def execute(self) -> int:
        self._prepare_stage()
        assert self.stage is not None
        status = "failed"
        try:
            try:
                returncode, stderr = self._run_runtime()
            finally:
                self._discard_cidfile()
            if self.cancelled:
                raise JobFailure("cancellation", "cascade session cancelled")
            metric_error: JobFailure | None = None
            try:
                self._execution_metrics(returncode, stderr)
            except JobFailure as error:
                metric_error = error
            if self.invalid_line is not None:
                raise JobFailure("event_stream", "runtime emitted malformed cascade JSONL")
            if metric_error is not None:
                raise metric_error
            terminal = self.validator.terminal
            if returncode != 0:
                message = (terminal.get("message") if terminal and terminal["event"] == "session_error"
                           else f"runtime exited with status {returncode}")
                raise JobFailure("runtime", message)
            if terminal is None or terminal["event"] != "session_completed":
                raise JobFailure("event_stream", "runtime did not emit session_completed")
            if self.validator.metrics is None:
                raise JobFailure("event_stream", "runtime did not emit terminal session metrics")
            if abs(self.validator.last_audio_position - self.audio_duration) > 0.05:
                raise JobFailure("event_stream", "terminal audio position disagrees with source duration")
            status = "complete"
        except JobFailure as error:
            status = "cancelled" if self.cancelled else "failed"
            self.failure = {"stage": error.stage, "message": str(error)}
            try:
                self._append_synthetic(
                    "session_cancelled" if self.cancelled else "session_error", str(error), error.stage
                )
            except ValueError:
                pass
        except Exception as error:
            status = "cancelled" if self.cancelled else "failed"
            self.failure = {"stage": "internal", "message": f"{type(error).__name__}: {error}"}
            try:
                self._append_synthetic(
                    "session_cancelled" if self.cancelled else "session_error",
                    self.failure["message"], "internal",
                )
            except ValueError:
                pass

        segments = {
            "schema_version": 1, "status": status,
            "session_id": self.session_id, "segments": self.validator.segments,
            "assembly": "ordered_authoritative_nonempty_segments_joined_with_one_space",
        }
        _write_json(self.stage / "segments.json", segments)
        if status == "complete":
            _write(
                self.stage / "transcript.txt",
                (assemble_transcript(self.validator.segments) + "\n").encode("utf-8"),
            )
        _write_json(self.stage / "result.json", self._result(status))
        try:
            _rename_noreplace(self.stage, self.output)
        except FileExistsError as error:
            raise JobFailure("publication", f"output path appeared during the job: {self.output}") from error
        self.stage = None
        return 0 if status == "complete" else (130 if status == "cancelled" else 1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/cascade",
        description="Experimental recorded-file Nemotron to Parakeet two-pass cascade.",
    )
    parser.add_argument("--output", required=True, type=Path, metavar="DIR",
                        help="new private audit-bundle directory (must not exist)")
    parser.add_argument("--pace", action="store_true",
                        help="feed source audio at real-time 20 ms cadence")
    parser.add_argument("audio", type=Path, metavar="AUDIO")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    manifest_path = Path(env.get("NATIVE_ASR_MODEL_MANIFEST", root / "manifests/models.lock"))
    try:
        audio = args.audio.expanduser().resolve(strict=True)
    except OSError:
        print(f"error: audio input does not exist: {args.audio}", file=sys.stderr)
        return 2
    output = args.output.expanduser().absolute()
    try:
        records = _manifest(manifest_path)
        job = CascadeJob(root, output, audio, records, args.pace, env)
        job.preflight()
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    previous_int = signal.signal(signal.SIGINT, job.cancel)
    previous_term = signal.signal(signal.SIGTERM, job.cancel)
    try:
        code = job.execute()
    except JobFailure as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if job.stage is not None:
            shutil.rmtree(job.stage, ignore_errors=True)
    if code != 0 and job.failure is not None:
        print(f"error: {job.failure['message']}; audit bundle: {output}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
