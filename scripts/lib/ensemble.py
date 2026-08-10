#!/usr/bin/env python3
"""Deterministic three-track ASR consensus and offline orchestration."""

from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import datetime as dt
import difflib
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, Iterable

from evaluation import NORMALIZATION_VERSION, normalize


DEFAULT_MODELS = (
    "nemo:parakeet-tdt-v3",
    "sherpa:parakeet-unified-en",
    "whisper:small.en",
)
RUNTIME_IMAGES = {
    "sherpa-onnx": "asr-sherpa-onnx",
    "nemo-speech": "asr-nemo-speech",
    "moonshine": "asr-moonshine",
    "whisper-cpp": "asr-whisper-cpp",
}
APOSTROPHES = {"’", "‘", "ʼ", "`", "'"}
TIME_RE = re.compile(
    r"^NATIVE_ASR_TIME\t([^\t]+)\t([^\t]+)\t([^\t]+)\t([^\t]+)$",
    re.MULTILINE,
)


class ValidationError(RuntimeError):
    """A command-line or preflight failure; no job has started."""


class JobFailure(RuntimeError):
    """A failure after the auditable job boundary."""

    def __init__(self, stage: str, message: str, model_alias: str | None = None):
        super().__init__(message)
        self.stage = stage
        self.model_alias = model_alias


def _lexical(character: str) -> bool:
    return character in APOSTROPHES or unicodedata.category(character)[:1] in {"L", "N", "M"}


def lexical_tokens(text: str) -> list[dict[str, Any]]:
    """Tokenize with the WER policy while retaining exact display material."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, character in enumerate(text):
        if _lexical(character):
            if start is None:
                start = index
        elif start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(text)))

    tokens: list[dict[str, Any]] = []
    for index, (begin, end) in enumerate(spans):
        surface = text[begin:end]
        normalized = normalize(surface)
        if not normalized:
            continue
        # The scanner uses the same lexical boundary rules as normalize, so a
        # span is one token. Keep a defensive split for unusual Unicode input.
        pieces = normalized.split()
        if len(pieces) != 1:
            raise ValueError(f"lexical span normalized to multiple tokens: {surface!r}")
        next_begin = spans[index + 1][0] if index + 1 < len(spans) else len(text)
        tokens.append({
            "index": len(tokens),
            "normalized": pieces[0],
            "surface": surface,
            "prefix": text[:begin] if not tokens else "",
            "separator_after": text[end:next_begin],
            "timing": None,
        })
    if [item["normalized"] for item in tokens] != normalize(text).split():
        raise ValueError("token extraction diverged from the normalization policy")
    return tokens


def _seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return None
    hours = float(match.group(1) or 0)
    return hours * 3600 + float(match.group(2)) * 60 + float(match.group(3))


def _native_units(result: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    words = result.get("words")
    if isinstance(words, list) and words:
        units = []
        for index, word in enumerate(words):
            if not isinstance(word, dict):
                continue
            text = word.get("word", word.get("text"))
            start, end = _seconds(word.get("start")), _seconds(word.get("end"))
            if isinstance(text, str) and start is not None and end is not None:
                units.append({"text": text, "start_seconds": start, "end_seconds": end,
                              "native_index": index})
        if units:
            return units, "word"

    segments = result.get("segments")
    if isinstance(segments, list) and segments:
        units = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            text = segment.get("text")
            start, end = _seconds(segment.get("start")), _seconds(segment.get("end"))
            if isinstance(text, str) and start is not None and end is not None:
                units.append({"text": text, "start_seconds": start, "end_seconds": end,
                              "native_index": index})
        if units:
            return units, "segment"

    raw = result.get("raw")
    transcription = raw.get("transcription") if isinstance(raw, dict) else None
    if isinstance(transcription, list) and transcription:
        units = []
        for index, segment in enumerate(transcription):
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                continue
            start = end = None
            timestamps = segment.get("timestamps")
            if isinstance(timestamps, dict):
                start = _seconds(timestamps.get("from"))
                end = _seconds(timestamps.get("to"))
            offsets = segment.get("offsets")
            if (start is None or end is None) and isinstance(offsets, dict):
                offset_start, offset_end = offsets.get("from"), offsets.get("to")
                if isinstance(offset_start, (int, float)) and isinstance(offset_end, (int, float)):
                    start, end = float(offset_start) / 1000, float(offset_end) / 1000
            if start is not None and end is not None:
                units.append({"text": segment["text"], "start_seconds": start,
                              "end_seconds": end, "native_index": index})
        if units:
            return units, "segment"
    return [], "unavailable"


def add_native_timing(result: dict[str, Any], tokens: list[dict[str, Any]]) -> str:
    """Attach only native word or segment spans that can be matched exactly."""
    units, basis = _native_units(result)
    if not units:
        return "unavailable"
    native: list[tuple[str, dict[str, Any]]] = []
    for unit in units:
        for normalized in normalize(unit["text"]).split():
            native.append((normalized, unit))
    token_words = [item["normalized"] for item in tokens]
    native_words = [item[0] for item in native]
    matcher = difflib.SequenceMatcher(None, token_words, native_words, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            token = tokens[block.a + offset]
            unit = native[block.b + offset][1]
            token["timing"] = {
                "basis": basis,
                "start_seconds": unit["start_seconds"],
                "end_seconds": unit["end_seconds"],
                "native_index": unit["native_index"],
            }
    return basis


def _local_alignment(
    left: list[int], right: list[int], left_words: list[str], right_words: list[str]
) -> list[tuple[int | None, int | None]]:
    """A deterministic Levenshtein alignment for one changed region."""
    rows, columns = len(left), len(right)
    costs = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(rows, -1, -1):
        costs[row][columns] = rows - row
    for column in range(columns, -1, -1):
        costs[rows][column] = columns - column
    for row in range(rows - 1, -1, -1):
        for column in range(columns - 1, -1, -1):
            substitution = costs[row + 1][column + 1] + (
                left_words[left[row]] != right_words[right[column]]
            )
            deletion = costs[row + 1][column] + 1
            insertion = costs[row][column + 1] + 1
            costs[row][column] = min(substitution, deletion, insertion)

    aligned: list[tuple[int | None, int | None]] = []
    row = column = 0
    while row < rows or column < columns:
        choices: list[tuple[int, int, str]] = []
        if row < rows and column < columns:
            choices.append((
                costs[row + 1][column + 1]
                + (left_words[left[row]] != right_words[right[column]]),
                0,
                "diagonal",
            ))
        if row < rows:
            choices.append((costs[row + 1][column] + 1, 1, "deletion"))
        if column < columns:
            choices.append((costs[row][column + 1] + 1, 2, "insertion"))
        _, _, operation = min(choices)
        if operation == "diagonal":
            aligned.append((left[row], right[column]))
            row += 1
            column += 1
        elif operation == "deletion":
            aligned.append((left[row], None))
            row += 1
        else:
            aligned.append((None, right[column]))
            column += 1
    return aligned


def pair_alignment(left_words: list[str], right_words: list[str]) -> list[tuple[int | None, int | None]]:
    """Exact matching blocks with local edit alignment between them."""
    matcher = difflib.SequenceMatcher(None, left_words, right_words, autojunk=False)
    result: list[tuple[int | None, int | None]] = []
    left_at = right_at = 0
    for block in matcher.get_matching_blocks():
        result.extend(_local_alignment(
            list(range(left_at, block.a)), list(range(right_at, block.b)),
            left_words, right_words,
        ))
        for offset in range(block.size):
            result.append((block.a + offset, block.b + offset))
        left_at, right_at = block.a + block.size, block.b + block.size
    return result


def _anchor_projection(
    anchor_words: list[str], other_words: list[str]
) -> tuple[list[int | None], list[list[int]]]:
    mapped: list[int | None] = [None] * len(anchor_words)
    insertions: list[list[int]] = [[] for _ in range(len(anchor_words) + 1)]
    gap = 0
    for anchor_index, other_index in pair_alignment(anchor_words, other_words):
        if anchor_index is None:
            assert other_index is not None
            insertions[gap].append(other_index)
        else:
            mapped[anchor_index] = other_index
            gap = anchor_index + 1
    return mapped, insertions


def alignment_columns(tracks: list[dict[str, Any]]) -> list[list[dict[str, Any] | None]]:
    if len(tracks) != 3:
        raise ValueError("consensus requires exactly three tracks")
    words = [[item["normalized"] for item in track["normalized_tokens"]] for track in tracks]
    second_map, second_insertions = _anchor_projection(words[0], words[1])
    third_map, third_insertions = _anchor_projection(words[0], words[2])
    columns: list[list[dict[str, Any] | None]] = []
    for gap in range(len(words[0]) + 1):
        insertion_alignment = _local_alignment(
            second_insertions[gap], third_insertions[gap], words[1], words[2]
        )
        for second_index, third_index in insertion_alignment:
            columns.append([
                None,
                tracks[1]["normalized_tokens"][second_index] if second_index is not None else None,
                tracks[2]["normalized_tokens"][third_index] if third_index is not None else None,
            ])
        if gap < len(words[0]):
            columns.append([
                tracks[0]["normalized_tokens"][gap],
                (tracks[1]["normalized_tokens"][second_map[gap]]
                 if second_map[gap] is not None else None),
                (tracks[2]["normalized_tokens"][third_map[gap]]
                 if third_map[gap] is not None else None),
            ])
    return columns


def _token_ref(alias: str, token: dict[str, Any] | None) -> dict[str, Any] | None:
    if token is None:
        return None
    return {
        "model_alias": alias,
        "track_token_index": token["index"],
        "normalized": token["normalized"],
        "surface": token["surface"],
        "timing": token["timing"],
    }


def _separator(previous: dict[str, Any], current: dict[str, Any]) -> str:
    prior_token, token = previous["token"], current["token"]
    if (previous["track_index"] == current["track_index"]
            and token["index"] == prior_token["index"] + 1):
        return prior_token["separator_after"]
    raw = prior_token["separator_after"]
    punctuation = "".join(character for character in raw if not character.isspace())
    if punctuation in {"-", "‐", "‑", "–", "—", "/"}:
        return punctuation
    return punctuation + " "


def render_selected(selected: list[dict[str, Any]], track_lengths: list[int]) -> str:
    if not selected:
        return ""
    first_token = selected[0]["token"]
    prefix = first_token["prefix"].strip()
    output = prefix + first_token["surface"]
    for previous, current in zip(selected, selected[1:]):
        output += _separator(previous, current) + current["token"]["surface"]
    last = selected[-1]
    if last["token"]["index"] == track_lengths[last["track_index"]] - 1:
        output += last["token"]["separator_after"].strip()
    return output.strip()


def _time_bounds(tokens: Iterable[dict[str, Any] | None]) -> dict[str, Any] | None:
    material = [token for token in tokens if token is not None]
    timed = [token["timing"] for token in material if token.get("timing") is not None]
    if not timed:
        return None
    bases = sorted({item["basis"] for item in timed})
    return {
        "start_seconds": min(item["start_seconds"] for item in timed),
        "end_seconds": max(item["end_seconds"] for item in timed),
        "basis": bases[0] if len(bases) == 1 else "mixed",
        "timed_tokens": len(timed),
        "tokens": len(material),
    }


def build_consensus(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    aliases = [track["model_alias"] for track in tracks]
    raw_columns = alignment_columns(tracks)
    columns: list[dict[str, Any]] = []
    selected_material: list[dict[str, Any]] = []
    counts = Counter()
    for index, raw in enumerate(raw_columns):
        values = [token["normalized"] if token is not None else None for token in raw]
        vote_counts = Counter(values)
        if len(vote_counts) == 1:
            selected_value = values[0]
            decision = "unanimous_token" if selected_value is not None else "unanimous_deletion"
        else:
            majority = next((value for value in values if vote_counts[value] >= 2), ...)
            if majority is not ...:
                selected_value = majority
                decision = "majority_deletion" if majority is None else "majority_token"
            else:
                selected_value = values[0]
                decision = "primary_fallback"
        selected_track = None
        selected_token = None
        supporters: list[str] = []
        if selected_value is not None:
            supporters = [aliases[i] for i, value in enumerate(values) if value == selected_value]
            selected_track = next(i for i, value in enumerate(values) if value == selected_value)
            selected_token = raw[selected_track]
            assert selected_token is not None
            selected_material.append({"track_index": selected_track, "token": selected_token,
                                      "column_index": index})
        counts[decision] += 1
        counts["columns"] += 1
        counts["selected_tokens" if selected_value is not None else "deleted_columns"] += 1
        if decision == "primary_fallback":
            counts["unresolved"] += 1
        if len(vote_counts) != 1:
            counts["non_unanimous"] += 1
        ordered_votes = []
        for value in values:
            if not any(item["normalized"] == value for item in ordered_votes):
                ordered_votes.append({
                    "normalized": value,
                    "count": vote_counts[value],
                    "models": [aliases[i] for i, candidate in enumerate(values)
                               if candidate == value],
                })
        columns.append({
            "index": index,
            "kind": "anchor" if raw[0] is not None else "insertion",
            "tracks": [_token_ref(aliases[i], token) for i, token in enumerate(raw)],
            "votes": ordered_votes,
            "selected": (None if selected_token is None else {
                **_token_ref(aliases[selected_track], selected_token),
                "supporting_models": supporters,
            }),
            "decision": decision,
            "unresolved": decision == "primary_fallback",
        })

    track_lengths = [len(track["normalized_tokens"]) for track in tracks]
    text = render_selected(selected_material, track_lengths)
    non_unanimous = [column["decision"] not in {"unanimous_token", "unanimous_deletion"}
                     for column in columns]
    spans: list[dict[str, Any]] = []
    span_start = 0
    while span_start < len(columns):
        if not non_unanimous[span_start]:
            span_start += 1
            continue
        span_end = span_start + 1
        while span_end < len(columns) and non_unanimous[span_end]:
            span_end += 1
        span_columns = raw_columns[span_start:span_end]
        selected_in_span = [item for item in selected_material
                            if span_start <= item["column_index"] < span_end]
        alternatives = []
        for track_index, alias in enumerate(aliases):
            tokens = [column[track_index] for column in span_columns
                      if column[track_index] is not None]
            alternatives.append({
                "model_alias": alias,
                "text": " ".join(token["surface"] for token in tokens),
                "normalized": " ".join(token["normalized"] for token in tokens),
                "time_bounds": _time_bounds(tokens),
            })
        before = [item["token"]["normalized"] for item in selected_material
                  if item["column_index"] < span_start][-5:]
        after = [item["token"]["normalized"] for item in selected_material
                 if item["column_index"] >= span_end][:5]
        spans.append({
            "index": len(spans),
            "start_column": span_start,
            "end_column_exclusive": span_end,
            "alternatives": alternatives,
            "selected": {
                "text": " ".join(item["token"]["surface"] for item in selected_in_span),
                "normalized": " ".join(item["token"]["normalized"] for item in selected_in_span),
                "time_bounds": _time_bounds(item["token"] for item in selected_in_span),
            },
            "context": {"before": " ".join(before), "after": " ".join(after)},
            "unresolved": any(column["unresolved"] for column in columns[span_start:span_end]),
        })
        span_start = span_end

    return {
        "text": text,
        "columns": columns,
        "disagreements": spans,
        "decision_counts": {key: counts.get(key, 0) for key in (
            "columns", "selected_tokens", "deleted_columns", "unanimous_token",
            "unanimous_deletion", "majority_token", "majority_deletion",
            "primary_fallback", "unresolved", "non_unanimous",
        )},
    }


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
    """Publish one directory atomically without ever replacing a race winner."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        if result == 0:
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
        digest.update(str(path.name).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_name(order: int, alias: str) -> str:
    return f"{order + 1:02d}-{re.sub(r'[^A-Za-z0-9_-]+', '-', alias).strip('-')}"


class EnsembleJob:
    def __init__(self, root: Path, output: Path, audio: Path, aliases: list[str],
                 records: dict[str, dict[str, str]], env: dict[str, str]):
        self.root, self.output, self.audio = root, output, audio
        self.aliases, self.records, self.env = aliases, records, env
        self.engine = env.get("NATIVE_ASR_CONTAINER_ENGINE", "docker")
        self.stage: Path | None = None
        self.current: subprocess.Popen[bytes] | None = None
        self.cancelled = False
        self.models: list[dict[str, Any]] = []
        self.job_started = 0.0
        self.created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.failure: dict[str, Any] | None = None
        self.audio_duration = 0.0
        self.audio_sha256 = ""
        self.git_revision = ""
        self.dirty_tree = False
        self.adapter_sha256 = ""

    def cancel(self, _signum: int, _frame: Any) -> None:
        self.cancelled = True
        if self.current is not None and self.current.poll() is None:
            try:
                os.killpg(self.current.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _communicate(self) -> tuple[bytes, bytes]:
        """Collect a child, escalating cancellation if Docker does not exit."""
        assert self.current is not None
        while True:
            try:
                return self.current.communicate(timeout=0.25)
            except subprocess.TimeoutExpired:
                if not self.cancelled:
                    continue
                try:
                    os.killpg(self.current.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    return self.current.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.current.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return self.current.communicate()

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
        transcribe = self.root / "scripts/transcribe"
        verify = self.root / "scripts/verify-models"
        if not os.access(transcribe, os.X_OK) or not os.access(verify, os.X_OK):
            raise ValidationError("transcription scripts are not executable")

        for alias in self.aliases:
            record = self.records.get(alias)
            if record is None:
                raise ValidationError(f"unknown model alias: {alias}")
            if record["runtime"] not in RUNTIME_IMAGES or alias.endswith(":silero-vad"):
                raise ValidationError(f"model is not transcribable: {alias}")

        _command_output([str(verify), *self.aliases], "model verification", self.env)
        try:
            logical_cpus = int(os.sysconf("SC_NPROCESSORS_ONLN"))
        except (OSError, ValueError):
            logical_cpus = os.cpu_count() or 1
        image_ids: dict[str, str] = {}
        for alias in self.aliases:
            record = self.records[alias]
            image = RUNTIME_IMAGES[record["runtime"]]
            if image not in image_ids:
                image_ids[image] = _command_output(
                    [self.engine, "image", "inspect", image, "--format", "{{.Id}}"],
                    f"container image inspection for {image}", self.env,
                ).splitlines()[-1]
                if not image_ids[image]:
                    raise ValidationError(f"container image inspection returned no ID: {image}")
            dependencies = []
            for dependency in filter(None, record["requires"].split(",")):
                if dependency not in self.records:
                    raise ValidationError(f"unknown dependency alias: {dependency}")
                dependencies.append(self._artifact(self.records[dependency]))
            threads = None if record["runtime"] == "moonshine" else (
                4 if record["runtime"] == "nemo-speech" else logical_cpus
            )
            name = _safe_name(len(self.models), alias)
            self.models.append({
                "order": len(self.models) + 1,
                "alias": alias,
                "runtime": record["runtime"],
                "anchor": not self.models,
                "artifacts": [self._artifact(record), *dependencies],
                "container": {"image": image, "image_id": image_ids[image]},
                "threads": threads,
                "thread_policy": "runtime-managed" if threads is None else "validated-default",
                "track": f"tracks/{name}.json",
                "stderr_log": f"logs/{name}.stderr.log",
                "status": "pending",
                "execution": None,
            })

        duration = _command_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", "--", str(self.audio),
        ], "audio duration probe", self.env)
        try:
            self.audio_duration = float(duration)
        except ValueError as error:
            raise ValidationError(f"audio duration is invalid: {duration}") from error
        if self.audio_duration <= 0:
            raise ValidationError(f"audio duration must be positive: {duration}")
        self.audio_sha256 = _sha256(self.audio)
        self.git_revision = _command_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], "Git revision lookup", self.env
        )
        self.dirty_tree = bool(_command_output(
            ["git", "-C", str(self.root), "status", "--porcelain"],
            "Git worktree lookup", self.env,
        ))
        self.adapter_sha256 = _adapter_sha256([
            self.root / "scripts/ensemble", self.root / "scripts/lib/ensemble.py",
            self.root / "scripts/lib/evaluation.py", self.root / "scripts/transcribe",
        ])

    @staticmethod
    def _artifact(record: dict[str, str]) -> dict[str, str]:
        return {key: record[key] for key in (
            "artifact_id", "alias", "revision", "sha256", "license", "filename",
        )}

    def _prepare_stage(self) -> None:
        self.stage = Path(tempfile.mkdtemp(
            prefix=f".{self.output.name}.native-asr.", dir=self.output.parent
        ))
        os.chmod(self.stage, 0o700)
        (self.stage / "tracks").mkdir(mode=0o700)
        (self.stage / "logs").mkdir(mode=0o700)
        for model in self.models:
            _write(self.stage / model["stderr_log"], b"")
            _write_json(self.stage / model["track"], {
                "schema_version": 1, "status": "pending", "model_alias": model["alias"],
                "timing_basis": "unavailable", "runtime_result": None,
                "normalized_tokens": [],
            })

    def _run_track(self, model: dict[str, Any]) -> dict[str, Any]:
        assert self.stage is not None
        alias = model["alias"]
        command = [str(self.root / "scripts/transcribe"), "--format", "json"]
        if model["threads"] is not None:
            command += ["--threads", str(model["threads"])]
        command += [alias, str(self.audio)]
        child_env = {**self.env, "LC_ALL": "C", "NATIVE_ASR_MODEL_VERIFIED_ALIAS": alias,
                     "NATIVE_ASR_MEASURE": "1"}
        print(f"ensemble: transcribing with {alias}", file=sys.stderr, flush=True)
        started = time.monotonic()
        model["status"] = "running"
        self.current = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env, start_new_session=True,
        )
        stdout_bytes, stderr_bytes = self._communicate()
        wall = time.monotonic() - started
        returncode = self.current.returncode
        self.current = None
        _write(self.stage / model["stderr_log"], stderr_bytes)
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if self.cancelled:
            model["status"] = "cancelled"
            model["execution"] = {"wall_seconds": wall, "exit_status": returncode,
                                  "user_seconds": None, "system_seconds": None,
                                  "cpu_seconds": None, "peak_rss_kb": None}
            _write_json(self.stage / model["track"], {
                "schema_version": 1, "status": "cancelled", "model_alias": alias,
                "timing_basis": "unavailable", "runtime_result": None,
                "normalized_tokens": [], "raw_stdout": stdout,
            })
            raise JobFailure("inference", "job cancelled", alias)

        metric_matches = list(TIME_RE.finditer(stderr))
        if not metric_matches:
            self._failed_track(model, stdout, wall, returncode,
                               "runtime emitted no timing metrics")
            raise JobFailure("inference", "runtime emitted no timing metrics", alias)
        metric = metric_matches[-1].groups()
        try:
            user_seconds, system_seconds = float(metric[0]), float(metric[1])
            peak_rss_kb, timed_exit = int(metric[2]), int(metric[3])
        except ValueError as error:
            self._failed_track(model, stdout, wall, returncode, "runtime timing metrics are malformed")
            raise JobFailure("inference", "runtime timing metrics are malformed", alias) from error
        model["execution"] = {
            "wall_seconds": wall,
            "user_seconds": user_seconds,
            "system_seconds": system_seconds,
            "cpu_seconds": user_seconds + system_seconds,
            "peak_rss_kb": peak_rss_kb,
            "exit_status": returncode,
        }
        if timed_exit != returncode:
            self._failed_track(model, stdout, wall, returncode,
                               f"runtime exit statuses disagree: {returncode} != {timed_exit}")
            raise JobFailure("inference", "runtime and timing exit statuses disagree", alias)
        if returncode:
            self._failed_track(model, stdout, wall, returncode,
                               f"transcription exited with status {returncode}")
            raise JobFailure("inference", f"transcription exited with status {returncode}", alias)
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as error:
            self._failed_track(model, stdout, wall, returncode, "transcription JSON is malformed")
            raise JobFailure("inference", "transcription JSON is malformed", alias) from error
        if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                or not result["text"].strip()):
            self._failed_track(model, stdout, wall, returncode,
                               "transcription hypothesis is empty or malformed")
            raise JobFailure("inference", "transcription hypothesis is empty or malformed", alias)
        artifact = result.get("model_artifact")
        if (result.get("model_alias") != alias or not isinstance(artifact, dict)
                or artifact.get("sha256") != model["artifacts"][0]["sha256"]):
            self._failed_track(model, stdout, wall, returncode,
                               "transcription provenance is missing or inconsistent")
            raise JobFailure("inference", "transcription provenance is missing or inconsistent", alias)
        try:
            tokens = lexical_tokens(result["text"])
        except ValueError as error:
            self._failed_track(model, stdout, wall, returncode, str(error))
            raise JobFailure("inference", str(error), alias) from error
        if not tokens:
            self._failed_track(model, stdout, wall, returncode, "transcription has no lexical tokens")
            raise JobFailure("inference", "transcription has no lexical tokens", alias)
        timing_basis = add_native_timing(result, tokens)
        track = {
            "schema_version": 1,
            "status": "complete",
            "model_alias": alias,
            "normalization": NORMALIZATION_VERSION,
            "timing_basis": timing_basis,
            "runtime_result": result,
            "normalized_tokens": tokens,
        }
        _write_json(self.stage / model["track"], track)
        model["status"] = "complete"
        return track

    def _failed_track(self, model: dict[str, Any], stdout: str, wall: float,
                      returncode: int, message: str) -> None:
        assert self.stage is not None
        model["status"] = "failed"
        if model["execution"] is None:
            model["execution"] = {"wall_seconds": wall, "exit_status": returncode,
                                  "user_seconds": None, "system_seconds": None,
                                  "cpu_seconds": None, "peak_rss_kb": None}
        _write_json(self.stage / model["track"], {
            "schema_version": 1, "status": "failed", "model_alias": model["alias"],
            "timing_basis": "unavailable", "runtime_result": None,
            "normalized_tokens": [], "raw_stdout": stdout, "error": message,
        })

    def _result(self, status: str, text: str | None,
                decisions: dict[str, int] | None) -> dict[str, Any]:
        executions = [model["execution"] for model in self.models if model["execution"]]
        completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        return {
            "schema_version": 1,
            "status": status,
            "created_at": self.created_at,
            "completed_at": completed_at,
            "text": text,
            "normalization": NORMALIZATION_VERSION,
            "source": {
                "path": str(self.audio), "filename": self.audio.name,
                "sha256": self.audio_sha256, "duration_seconds": self.audio_duration,
            },
            "models": self.models,
            "provenance": {
                "git_revision": self.git_revision,
                "git_dirty": self.dirty_tree,
                "adapter_sha256": self.adapter_sha256,
            },
            "timing": {
                "wall_seconds": time.monotonic() - self.job_started,
                "cpu_seconds": (sum(item["cpu_seconds"] for item in executions
                                    if item["cpu_seconds"] is not None) if executions else 0),
                "peak_rss_kb": (max((item["peak_rss_kb"] for item in executions
                                    if item["peak_rss_kb"] is not None), default=None)),
            },
            "decision_counts": decisions,
            "failure": self.failure,
            "artifacts": {
                "transcript": "transcript.txt" if status == "complete" else None,
                "alignment": "alignment.json", "disagreements": "disagreements.json",
            },
        }

    def execute(self) -> tuple[int, str | None]:
        self._prepare_stage()
        assert self.stage is not None
        self.job_started = time.monotonic()
        tracks: list[dict[str, Any]] = []
        status, text, decisions = "failed", None, None
        try:
            for model in self.models:
                if self.cancelled:
                    raise JobFailure("inference", "job cancelled")
                tracks.append(self._run_track(model))
            if self.cancelled:
                raise JobFailure("consensus", "job cancelled")
            consensus = build_consensus(tracks)
            text = consensus["text"]
            if not normalize(text):
                raise JobFailure("consensus", "consensus produced an empty transcript")
            alignment = {
                "schema_version": 1, "status": "complete", "anchor_model": self.aliases[0],
                "models": self.aliases, "normalization": NORMALIZATION_VERSION,
                "columns": consensus["columns"], "decision_counts": consensus["decision_counts"],
            }
            disagreements = {
                "schema_version": 1, "status": "complete", "models": self.aliases,
                "spans": consensus["disagreements"],
            }
            _write_json(self.stage / "alignment.json", alignment)
            _write_json(self.stage / "disagreements.json", disagreements)
            _write(self.stage / "transcript.txt", (text + "\n").encode("utf-8"))
            decisions = consensus["decision_counts"]
            status = "complete"
        except JobFailure as error:
            status = "cancelled" if self.cancelled else "failed"
            self.failure = {"stage": error.stage, "model_alias": error.model_alias,
                            "message": str(error)}
            unavailable = {"schema_version": 1, "status": "unavailable",
                           "reason": str(error), "models": self.aliases}
            _write_json(self.stage / "alignment.json", {**unavailable, "columns": []})
            _write_json(self.stage / "disagreements.json", {**unavailable, "spans": []})
        except Exception as error:  # Preserve audit evidence for internal failures too.
            status = "cancelled" if self.cancelled else "failed"
            self.failure = {"stage": "internal", "model_alias": None,
                            "message": f"{type(error).__name__}: {error}"}
            unavailable = {"schema_version": 1, "status": "unavailable",
                           "reason": self.failure["message"], "models": self.aliases}
            _write_json(self.stage / "alignment.json", {**unavailable, "columns": []})
            _write_json(self.stage / "disagreements.json", {**unavailable, "spans": []})
        _write_json(self.stage / "result.json", self._result(status, text, decisions))
        try:
            _rename_noreplace(self.stage, self.output)
        except FileExistsError as error:
            raise JobFailure("publication", f"output path appeared during the job: {self.output}") from error
        self.stage = None
        return (0 if status == "complete" else (130 if status == "cancelled" else 1), text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/ensemble",
        description="Run three offline ASR models sequentially and publish deterministic consensus.",
    )
    parser.add_argument("--output", required=True, type=Path, metavar="DIR",
                        help="new audit-bundle directory (must not exist)")
    parser.add_argument("--model", action="append", default=[], metavar="ALIAS",
                        help="ordered model alias; specify exactly three times")
    parser.add_argument("audio", type=Path, metavar="AUDIO")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    aliases = args.model or list(DEFAULT_MODELS)
    if len(aliases) != 3:
        print("error: --model overrides require exactly three aliases", file=sys.stderr)
        return 2
    if len(set(aliases)) != 3:
        print("error: the three model aliases must be distinct", file=sys.stderr)
        return 2
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
        job = EnsembleJob(root, output, audio, aliases, records, env)
        job.preflight()
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    previous_int = signal.signal(signal.SIGINT, job.cancel)
    previous_term = signal.signal(signal.SIGTERM, job.cancel)
    try:
        code, text = job.execute()
    except JobFailure as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if job.stage is not None:
            shutil.rmtree(job.stage, ignore_errors=True)
    if code == 0:
        assert text is not None
        print(text)
    elif job.failure is not None:
        print(f"error: {job.failure['message']}; audit bundle: {output}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
