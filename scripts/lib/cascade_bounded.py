#!/usr/bin/env python3
"""Bounded, checkpointed evaluation gate for the two-pass cascade."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable
import wave

from batch_adapter import _json_text, _runtime_json_payload
from cascade import (
    ENDPOINT_POLICY, ENDPOINT_SILENCE_MILLISECONDS, NEMOTRON_ALIAS,
    PARAKEET_ALIAS, _adapter_sha256,
)
from evaluation import NORMALIZATION_VERSION, errors, normalize


SCHEMA_VERSION = 1
RECIPE_VERSION = "cascade-bounded-pcm16-pairs-v1"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
GAP_SECONDS = 1.6
GAP_FRAMES = 25_600
SPLITS = ("librispeech-test-clean", "librispeech-test-other")
MODES = ("cascade", "parakeet", "nemotron")
IMAGE = "asr-nemo-speech"
TIME_RE = re.compile(r"^NATIVE_ASR_TIME\t([^\t]+)\t([^\t]+)\t([^\t]+)\t([^\t]+)$", re.M)
ENDPOINT_SILENCE_SECONDS = ENDPOINT_SILENCE_MILLISECONDS / 1_000
PILOT_MAX_EXTRA_ENDPOINT_PAIR_RATE = 0.20
BASELINE_MODES = ("parakeet", "nemotron")


class BenchmarkError(RuntimeError):
    pass


class ModeTimeout(BenchmarkError):
    pass


class DeadlineExceeded(BenchmarkError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, canonical(value) + b"\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def ranked(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (
        hashlib.sha256(row["utterance_id"].encode()).hexdigest(), row["utterance_id"]
    ))[:limit]


def deterministic_pairs(rows: list[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    if len(rows) % 2:
        raise BenchmarkError(f"selected utterance count for {split} must be even")
    result = []
    for index in range(0, len(rows), 2):
        members = rows[index:index + 2]
        result.append({
            "pair_id": f"{split.removeprefix('librispeech-')}-pair-{index // 2 + 1:03d}",
            "split": split.removeprefix("librispeech-"),
            "dataset": split,
            "members": members,
            "reference": " ".join(row["reference"].strip() for row in members).strip(),
        })
    return result


def wav_info(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as audio:
        result = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes())
        if audio.getcomptype() != "NONE":
            raise BenchmarkError(f"prepared input is compressed: {path}")
        return result


def _copy_wav_frames(destination: wave.Wave_write, source_path: Path) -> int:
    with wave.open(str(source_path), "rb") as source:
        parameters = (source.getnchannels(), source.getsampwidth(), source.getframerate())
        if parameters != (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE) or source.getcomptype() != "NONE":
            raise BenchmarkError(f"prepared input is not 16 kHz mono PCM16: {source_path}")
        count = source.getnframes()
        while True:
            frames = source.readframes(SAMPLE_RATE)
            if not frames:
                break
            destination.writeframesraw(frames)
        return count


def write_pair_wav(path: Path, first: Path, second: Path) -> tuple[int, int]:
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(raw)
    try:
        with wave.open(str(temporary), "wb") as destination:
            destination.setnchannels(CHANNELS)
            destination.setsampwidth(SAMPLE_WIDTH)
            destination.setframerate(SAMPLE_RATE)
            first_frames = _copy_wav_frames(destination, first)
            destination.writeframesraw(b"\0" * GAP_FRAMES * SAMPLE_WIDTH)
            second_frames = _copy_wav_frames(destination, second)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return first_frames, second_frames
    finally:
        temporary.unlink(missing_ok=True)


def load_dataset_manifest(datasets_root: Path, dataset: str) -> tuple[Path, list[dict[str, Any]]]:
    path = datasets_root / "manifests" / f"{dataset}.jsonl"
    if not path.is_file():
        raise BenchmarkError(f"prepared dataset manifest is missing: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    required = {"utterance_id", "prepared_path", "reference", "duration_seconds", "source_sha256"}
    for row in rows:
        if not required.issubset(row):
            raise BenchmarkError(f"prepared dataset manifest has an incomplete row: {path}")
        if not Path(row["prepared_path"]).is_file():
            raise BenchmarkError(f"prepared audio is missing: {row['prepared_path']}")
    return path, rows


def prepare_pair_cache(datasets_root: Path, cache_root: Path, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {}
    manifest_digests: dict[str, str] = {}
    for split in SPLITS:
        path, rows = load_dataset_manifest(datasets_root, split)
        selected[split] = ranked(rows, limit)
        manifest_digests[split] = sha256(path)
    identity = {
        "recipe_version": RECIPE_VERSION,
        "sample_rate": SAMPLE_RATE,
        "sample_width_bytes": SAMPLE_WIDTH,
        "channels": CHANNELS,
        "gap_frames": GAP_FRAMES,
        "ranking": "sha256(utterance_id)",
        "limit_per_split": limit,
        "dataset_manifests": manifest_digests,
        "sources": {
            split: [{key: row[key] for key in (
                "utterance_id", "source_sha256", "reference", "duration_seconds", "prepared_path"
            )} for row in rows] for split, rows in selected.items()
        },
    }
    cache_fingerprint = fingerprint(identity)
    destination = cache_root / cache_fingerprint
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    manifest_path = destination / "manifest.json"
    if manifest_path.is_file():
        existing = read_json(manifest_path)
        if existing.get("cache_fingerprint") != cache_fingerprint or existing.get("identity") != identity:
            raise BenchmarkError(f"pair cache fingerprint mismatch: {manifest_path}")
        for pair in existing.get("pairs", []):
            audio = Path(pair["audio_path"])
            if not audio.is_file() or sha256(audio) != pair["audio_sha256"]:
                raise BenchmarkError(f"cached pair is missing or corrupt: {audio}")
        return existing, existing["pairs"]

    pairs: list[dict[str, Any]] = []
    for split in SPLITS:
        for pair in deterministic_pairs(selected[split], split):
            audio_path = destination / f"{pair['pair_id']}.wav"
            first, second = (Path(row["prepared_path"]) for row in pair["members"])
            first_frames, second_frames = write_pair_wav(audio_path, first, second)
            first_end = first_frames / SAMPLE_RATE
            gap_end = first_end + GAP_SECONDS
            duration = gap_end + second_frames / SAMPLE_RATE
            members = []
            for row, frames in zip(pair["members"], (first_frames, second_frames), strict=True):
                members.append({
                    "utterance_id": row["utterance_id"], "source_path": row.get("source_path"),
                    "prepared_path": row["prepared_path"], "source_sha256": row["source_sha256"],
                    "prepared_sha256": sha256(Path(row["prepared_path"])),
                    "reference": row["reference"], "duration_seconds": frames / SAMPLE_RATE,
                    "speaker": row.get("speaker"),
                })
            record = {
                "pair_id": pair["pair_id"], "split": pair["split"], "dataset": split,
                "pair_fingerprint": fingerprint({
                    "recipe_version": RECIPE_VERSION,
                    "members": [(row["utterance_id"], row["source_sha256"]) for row in members],
                    "gap_frames": GAP_FRAMES,
                }),
                "members": members, "reference_raw": pair["reference"],
                "reference_normalized": normalize(pair["reference"]),
                "audio_path": str(audio_path.resolve()), "audio_sha256": sha256(audio_path),
                "duration_seconds": duration,
                "boundaries": {
                    "first": {"start_seconds": 0.0, "end_seconds": first_end},
                    "gap": {"start_seconds": first_end, "end_seconds": gap_end,
                            "frames": GAP_FRAMES},
                    "second": {"start_seconds": gap_end, "end_seconds": duration},
                },
            }
            pairs.append(record)
    manifest = {
        "schema_version": SCHEMA_VERSION, "cache_fingerprint": cache_fingerprint,
        "created_at": utc_now(), "identity": identity, "pairs": pairs,
    }
    atomic_json(manifest_path, manifest)
    return manifest, pairs


def prepare_stream(cache_manifest: dict[str, Any], cache_root: Path,
                   datasets_root: Path, max_seconds: float) -> dict[str, Any]:
    _, rows = load_dataset_manifest(datasets_root, "librispeech-test-other")
    limit = int(cache_manifest["identity"]["limit_per_split"])
    candidates = ranked(rows, limit)
    chosen: list[dict[str, Any]] = []
    total_frames = 0
    for row in candidates:
        info = wav_info(Path(row["prepared_path"]))
        if info[:3] != (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE):
            raise BenchmarkError(f"prepared input is not 16 kHz mono PCM16: {row['prepared_path']}")
        frames = info[3]
        proposed = total_frames + (GAP_FRAMES if chosen else 0) + frames
        if proposed / SAMPLE_RATE >= max_seconds:
            break
        chosen.append({**row, "frames": frames})
        total_frames = proposed
    if len(chosen) < 2:
        raise BenchmarkError("not enough complete test-other utterances for a paced stream")
    identity = {
        "recipe_version": RECIPE_VERSION, "kind": "paced-test-other",
        "pair_cache_fingerprint": cache_manifest["cache_fingerprint"],
        "max_seconds_exclusive": max_seconds, "gap_frames": GAP_FRAMES,
        "utterances": [[row["utterance_id"], row["source_sha256"]] for row in chosen],
    }
    stream_fingerprint = fingerprint(identity)
    directory = cache_root / cache_manifest["cache_fingerprint"]
    audio_path = directory / f"paced-{stream_fingerprint}.wav"
    manifest_path = directory / f"paced-{stream_fingerprint}.json"
    if manifest_path.is_file():
        existing = read_json(manifest_path)
        if existing.get("identity") != identity or not audio_path.is_file() or sha256(audio_path) != existing["audio_sha256"]:
            raise BenchmarkError(f"paced stream cache mismatch: {manifest_path}")
        return existing
    descriptor, raw = tempfile.mkstemp(prefix=f".{audio_path.name}.", dir=directory)
    os.close(descriptor)
    temporary = Path(raw)
    speech, gaps = [], []
    cursor = 0
    try:
        with wave.open(str(temporary), "wb") as destination:
            destination.setnchannels(CHANNELS); destination.setsampwidth(SAMPLE_WIDTH)
            destination.setframerate(SAMPLE_RATE)
            for index, row in enumerate(chosen):
                if index:
                    gap_start = cursor / SAMPLE_RATE
                    destination.writeframesraw(b"\0" * GAP_FRAMES * SAMPLE_WIDTH)
                    cursor += GAP_FRAMES
                    gaps.append({"start_seconds": gap_start, "end_seconds": cursor / SAMPLE_RATE})
                start = cursor / SAMPLE_RATE
                copied = _copy_wav_frames(destination, Path(row["prepared_path"]))
                cursor += copied
                speech.append({
                    "utterance_id": row["utterance_id"], "reference": row["reference"],
                    "source_sha256": row["source_sha256"], "start_seconds": start,
                    "end_seconds": cursor / SAMPLE_RATE,
                })
        os.chmod(temporary, 0o600)
        os.replace(temporary, audio_path)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION, "stream_fingerprint": stream_fingerprint,
        "identity": identity, "audio_path": str(audio_path.resolve()),
        "audio_sha256": sha256(audio_path), "duration_seconds": cursor / SAMPLE_RATE,
        "speech": speech, "gaps": gaps,
        "reference_raw": " ".join(row["reference"].strip() for row in chosen),
    }
    atomic_json(manifest_path, manifest)
    return manifest


def model_records(path: Path) -> dict[str, dict[str, str]]:
    keys = ("artifact_id", "alias", "runtime", "name", "source", "revision", "filename",
            "destination", "sha256", "license", "packaging", "requires", "notes")
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            row = dict(zip(keys, line.split("|"), strict=True))
            result[row["alias"]] = row
    for alias in (PARAKEET_ALIAS, NEMOTRON_ALIAS):
        if alias not in result or result[alias]["runtime"] != "nemo-speech":
            raise BenchmarkError(f"required model is not locked: {alias}")
    return result


def git_output(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def adapter_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in (
        "scripts/benchmark-cascade-bounded", "scripts/lib/cascade_bounded.py",
        "scripts/cascade", "scripts/lib/cascade.py", "scripts/lib/evaluation.py",
        "docker/nemo-speech/cascade-boundary.h",
        "docker/nemo-speech/native-asr-cascade.cpp", "docker/nemo-speech/Dockerfile",
        "docker/nemo-speech/endpoint-diagnostics.patch",
    ):
        digest.update(relative.encode() + b"\0")
        digest.update((root / relative).read_bytes() + b"\0")
    return digest.hexdigest()


def cascade_adapter_digest(root: Path) -> str:
    return _adapter_sha256([
        root / "scripts/cascade", root / "scripts/lib/cascade.py",
        root / "docker/nemo-speech/entrypoint.sh",
        root / "docker/nemo-speech/cascade-boundary.h",
        root / "docker/nemo-speech/native-asr-cascade.cpp",
        root / "docker/nemo-speech/endpoint-diagnostics.patch",
        root / "docker/nemo-speech/Dockerfile",
    ])


def _fake_baseline_components(variant: str = "default") -> dict[str, Any]:
    def component(kind: str, path: str) -> dict[str, str]:
        return {"path": path, "sha256": hashlib.sha256(
            f"fake-baseline-component:{variant}:{kind}:{path}".encode()
        ).hexdigest()}

    value = {
        "schema_version": SCHEMA_VERSION,
        "nemo_cli": component("nemo_cli", "/opt/native-asr/bin/nemo-speech"),
        "linked_native_libraries": [
            component("linked_native_library", "/opt/native-asr/lib/libnemo_speech_asr.so"),
        ],
        "timing_binary": component("timing_binary", "/usr/bin/time"),
    }
    value["components_fingerprint"] = fingerprint(value)
    return value


def baseline_component_fingerprints(engine: str, image: str,
                                    fake_variant: str | None = None) -> dict[str, Any]:
    """Hash every executable/library component used by a baseline process."""
    if fake_variant is not None:
        return _fake_baseline_components(fake_variant)
    script = r'''
set -eu
cli=/opt/native-asr/bin/nemo-speech
timer=/usr/bin/time
emit() {
    kind=$1
    path=$(readlink -f "$2")
    set -- $(sha256sum "$path")
    printf '%s\t%s\t%s\n' "$kind" "$path" "$1"
}
emit nemo_cli "$cli"
ldd "$cli" | awk '$2 == "=>" && $3 ~ /^\// {print $3} $1 ~ /^\// {print $1}' | sort -u |
while IFS= read -r library; do
    emit linked_native_library "$library"
done
emit timing_binary "$timer"
'''
    process = subprocess.run(
        [engine, "run", "--rm", "--network", "none", "--read-only",
         "--entrypoint", "/bin/sh", image, "-c", script],
        text=True, capture_output=True,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
        raise BenchmarkError(f"cannot fingerprint baseline components in {image}: {detail}")
    grouped: dict[str, list[dict[str, str]]] = {
        "nemo_cli": [], "linked_native_library": [], "timing_binary": [],
    }
    for line in process.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] not in grouped or not re.fullmatch(r"[0-9a-f]{64}", fields[2]):
            raise BenchmarkError(f"malformed baseline component fingerprint from {image}")
        grouped[fields[0]].append({"path": fields[1], "sha256": fields[2]})
    if len(grouped["nemo_cli"]) != 1 or len(grouped["timing_binary"]) != 1 or not grouped["linked_native_library"]:
        raise BenchmarkError(f"incomplete baseline component fingerprints from {image}")
    libraries = sorted(grouped["linked_native_library"], key=lambda row: row["path"])
    value = {
        "schema_version": SCHEMA_VERSION,
        "nemo_cli": grouped["nemo_cli"][0],
        "linked_native_libraries": libraries,
        "timing_binary": grouped["timing_binary"][0],
    }
    value["components_fingerprint"] = fingerprint(value)
    return value


def provenance(root: Path, records: dict[str, dict[str, str]], engine: str,
               fake: bool = False) -> dict[str, Any]:
    revision = git_output(root, "rev-parse", "HEAD")
    dirty = bool(git_output(root, "status", "--porcelain"))
    image_id = "sha256:fake-cascade-bounded" if fake else subprocess.check_output(
        [engine, "image", "inspect", IMAGE, "--format", "{{.Id}}"], text=True
    ).strip().splitlines()[-1]
    fake_variant = os.environ.get("NATIVE_ASR_TEST_BASELINE_COMPONENT_VARIANT", "default")
    return {
        "git_revision": revision, "git_dirty": dirty, "adapter_sha256": adapter_digest(root),
        "cascade_adapter_sha256": cascade_adapter_digest(root),
        "image": IMAGE, "image_id": image_id,
        "baseline_component_fingerprints": baseline_component_fingerprints(
            engine, image_id, fake_variant if fake else None
        ),
        "models": {alias: {key: records[alias][key] for key in (
            "artifact_id", "alias", "revision", "sha256", "filename", "license"
        )} for alias in (NEMOTRON_ALIAS, PARAKEET_ALIAS)},
    }


def wer_record(reference: str, hypothesis: str) -> dict[str, Any]:
    return {
        "reference_raw": reference, "hypothesis_raw": hypothesis,
        "reference_normalized": normalize(reference),
        "hypothesis_normalized": normalize(hypothesis),
        "wer_counts": errors(reference, hypothesis),
    }


def _sum_counts(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    keys = ("errors", "substitutions", "deletions", "insertions", "reference_words")
    result = {key: 0 for key in keys}
    for record in records:
        counts = record["wer_counts"]
        for key in keys:
            result[key] += int(counts[key])
    return result


def _wer(counts: dict[str, int]) -> float | None:
    return counts["errors"] / counts["reference_words"] if counts["reference_words"] else None


def baseline_options(options: dict[str, Any]) -> dict[str, Any]:
    """Return only selection/normalization/runtime options that affect baselines."""
    common_runtime = {
        "entrypoint": "/usr/bin/time",
        "timing_binary": "/usr/bin/time",
        "cli": "/opt/native-asr/bin/nemo-speech",
        "command": "transcribe",
        "input": "/audio",
        "output_dir": "/output",
        "device": "cpu",
        "format": "json",
        "word_times": True,
        "concurrency": 1,
        "force": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "recipe_version": RECIPE_VERSION,
        "selection": {
            key: options[key] for key in (
                "limit_per_split", "pairs_per_split", "ranking", "pairing", "gap_frames",
            )
        },
        "normalization": options["normalization"],
        "modes": {
            "parakeet": {**common_runtime, "model_alias": PARAKEET_ALIAS, "stream": False},
            "nemotron": {**common_runtime, "model_alias": NEMOTRON_ALIAS, "stream": True},
        },
    }


class BaselineReuse:
    """Read-only validator/importer for baseline details from an older run."""

    def __init__(self, source_root: Path, cache_manifest: dict[str, Any],
                 pairs: list[dict[str, Any]], current_provenance: dict[str, Any],
                 current_baseline_options: dict[str, Any], engine: str, fake: bool):
        self.source_root = source_root.expanduser().resolve()
        self.cache_manifest = cache_manifest
        self.current_pairs = {pair["pair_id"]: pair for pair in pairs}
        self.current_provenance = current_provenance
        self.current_baseline_options = current_baseline_options
        self.engine, self.fake = engine, fake
        self.source_manifest: dict[str, Any] = {}
        self.source_identity: dict[str, Any] = {}
        self.source_pairs: dict[str, dict[str, Any]] = {}
        self.source_components: dict[str, Any] | None = None
        self.global_error: str | None = None
        self.report_path: Path | None = None
        self.report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "requested_from": str(self.source_root),
            "imported": [], "rejected": [],
        }
        try:
            self._validate_source()
        except (BenchmarkError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.global_error = str(error)
        self.report["source_run_fingerprint"] = self.source_manifest.get("run_fingerprint")
        self.report["eligible"] = self.global_error is None
        self.report["global_rejection"] = self.global_error
        self.report["source_baseline_component_fingerprints"] = self.source_components
        self.report["current_baseline_component_fingerprints"] = current_provenance.get(
            "baseline_component_fingerprints"
        )

    def _validate_source(self) -> None:
        manifest_path = self.source_root / "run_manifest.json"
        if not manifest_path.is_file():
            raise BenchmarkError(f"baseline reuse run manifest is missing: {manifest_path}")
        self.source_manifest = read_json(manifest_path)
        source_run_fingerprint = self.source_manifest.get("run_fingerprint")
        if not isinstance(source_run_fingerprint, str) or not source_run_fingerprint:
            raise BenchmarkError("baseline reuse run fingerprint is missing")
        self.source_identity = self.source_manifest.get("identity")
        if not isinstance(self.source_identity, dict):
            raise BenchmarkError("baseline reuse identity is missing")
        if fingerprint(self.source_identity) != source_run_fingerprint:
            raise BenchmarkError("baseline reuse run fingerprint does not match its identity")
        if (self.source_identity.get("schema_version") != SCHEMA_VERSION or
                self.source_identity.get("recipe_version") != RECIPE_VERSION):
            raise BenchmarkError("baseline reuse schema or recipe changed")
        if self.source_identity.get("cache_fingerprint") != self.cache_manifest["cache_fingerprint"]:
            raise BenchmarkError("baseline reuse cache fingerprint mismatch")

        source_options = self.source_identity.get("options")
        if not isinstance(source_options, dict):
            raise BenchmarkError("baseline reuse options are missing")
        recorded_baseline_options = self.source_identity.get("baseline_options")
        if recorded_baseline_options is None:
            recorded_baseline_options = baseline_options(source_options)
        if recorded_baseline_options != self.current_baseline_options:
            raise BenchmarkError("baseline reuse options mismatch")
        if source_options.get("normalization") != NORMALIZATION_VERSION:
            raise BenchmarkError("baseline reuse normalization mismatch")

        source_provenance = self.source_identity.get("provenance")
        if not isinstance(source_provenance, dict):
            raise BenchmarkError("baseline reuse provenance is missing")
        if source_provenance.get("models") != self.current_provenance.get("models"):
            raise BenchmarkError("baseline reuse model artifacts mismatch")
        image_id = source_provenance.get("image_id")
        if not isinstance(image_id, str) or not image_id:
            raise BenchmarkError("baseline reuse image ID is missing")
        recorded_components = source_provenance.get("baseline_component_fingerprints")
        if self.fake:
            if not isinstance(recorded_components, dict):
                raise BenchmarkError("fake baseline reuse component fingerprints are missing")
            self.source_components = recorded_components
        else:
            self.source_components = baseline_component_fingerprints(self.engine, image_id)
            if recorded_components is not None and recorded_components != self.source_components:
                raise BenchmarkError("recorded baseline component fingerprints changed")
        if self.source_components != self.current_provenance.get("baseline_component_fingerprints"):
            raise BenchmarkError("baseline runtime component fingerprints mismatch")

        cache_path_raw = self.source_manifest.get("pair_cache_manifest")
        if not isinstance(cache_path_raw, str):
            raise BenchmarkError("baseline reuse cache manifest path is missing")
        source_cache = read_json(Path(cache_path_raw))
        if (source_cache.get("cache_fingerprint") != self.cache_manifest["cache_fingerprint"] or
                source_cache.get("identity") != self.cache_manifest.get("identity")):
            raise BenchmarkError("baseline reuse cache manifest mismatch")
        if fingerprint(source_cache["identity"]) != source_cache["cache_fingerprint"]:
            raise BenchmarkError("baseline reuse cache fingerprint does not match its identity")
        self.source_pairs = {
            pair["pair_id"]: pair for pair in source_cache.get("pairs", [])
            if isinstance(pair, dict) and isinstance(pair.get("pair_id"), str)
        }
        if set(self.source_pairs) != set(self.current_pairs):
            raise BenchmarkError("baseline reuse pair set mismatch")

    def identity(self) -> dict[str, Any]:
        return {
            "requested_from": str(self.source_root),
            "source_run_fingerprint": self.source_manifest.get("run_fingerprint"),
            "eligible": self.global_error is None,
            "global_rejection": self.global_error,
            "source_baseline_components_fingerprint": (
                self.source_components or {}
            ).get("components_fingerprint"),
        }

    def attach(self, run_root: Path) -> None:
        self.report_path = run_root / "baseline_reuse.json"
        if self.report_path.is_file():
            existing = read_json(self.report_path)
            if (existing.get("requested_from") != str(self.source_root) or
                    existing.get("source_run_fingerprint") !=
                    self.source_manifest.get("run_fingerprint")):
                raise BenchmarkError("baseline reuse checkpoint mismatch")
            self.report = existing
        else:
            self._checkpoint()

    def _checkpoint(self) -> None:
        if self.report_path is not None:
            self.report["updated_at"] = utc_now()
            atomic_json(self.report_path, self.report)

    def _reject(self, key: str, message: str) -> None:
        if not any(row.get("key") == key for row in self.report["rejected"]):
            self.report["rejected"].append({"key": key, "reason": message})
            self._checkpoint()

    def mark_imported(self, key: str, source_detail: str) -> None:
        if not any(item.get("key") == key for item in self.report["imported"]):
            self.report["imported"].append({"key": key, "source_detail": source_detail})
            self._checkpoint()

    def _validate_pair(self, pair: dict[str, Any]) -> None:
        old = self.source_pairs.get(pair["pair_id"])
        if old is None:
            raise BenchmarkError("source pair is missing")
        if old.get("pair_fingerprint") != pair["pair_fingerprint"]:
            raise BenchmarkError("pair fingerprint mismatch")
        if old.get("audio_sha256") != pair["audio_sha256"]:
            raise BenchmarkError("pair audio digest mismatch")
        for label, record in (("source", old), ("current", pair)):
            audio_path = Path(record["audio_path"])
            if not audio_path.is_file() or sha256(audio_path) != record["audio_sha256"]:
                raise BenchmarkError(f"{label} pair audio is missing or corrupt")

    def import_detail(self, pair: dict[str, Any], mode: str) -> dict[str, Any] | None:
        key = f"{pair['pair_id']}:{mode}"
        if self.global_error is not None:
            self._reject(key, self.global_error)
            return None
        try:
            if mode not in BASELINE_MODES:
                raise BenchmarkError("only baseline modes may be reused")
            self._validate_pair(pair)
            detail_path = self.source_root / "details" / pair["split"] / pair["pair_id"] / f"{mode}.json"
            if not detail_path.is_file():
                raise BenchmarkError("source baseline detail is missing")
            row = read_json(detail_path)
            alias = PARAKEET_ALIAS if mode == "parakeet" else NEMOTRON_ALIAS
            if (row.get("run_fingerprint") != self.source_manifest["run_fingerprint"] or
                    row.get("pair_fingerprint") != pair["pair_fingerprint"] or
                    row.get("pair_id") != pair["pair_id"] or row.get("split") != pair["split"]):
                raise BenchmarkError("source baseline detail fingerprint mismatch")
            if (row.get("status") != "complete" or row.get("failure") is not None or
                    row.get("mode") != mode):
                raise BenchmarkError("source baseline status is not successful")
            if row.get("model_load_count") != 1:
                raise BenchmarkError("source baseline model-load count is not one")
            timing = row.get("timing")
            if (not isinstance(timing, dict) or timing.get("exit_status") != 0 or
                    not isinstance(timing.get("wall_seconds"), (int, float)) or
                    isinstance(timing.get("wall_seconds"), bool) or
                    not math.isfinite(timing["wall_seconds"]) or timing["wall_seconds"] < 0):
                raise BenchmarkError("source baseline timing is not successful")
            source_provenance = self.source_identity["provenance"]
            detail_provenance = row.get("provenance")
            if (not isinstance(detail_provenance, dict) or
                    detail_provenance.get("git_revision") != source_provenance.get("git_revision") or
                    detail_provenance.get("image_id") != source_provenance.get("image_id") or
                    detail_provenance.get("model") != source_provenance["models"][alias]):
                raise BenchmarkError("source baseline provenance mismatch")
            hypothesis = row.get("hypothesis_raw")
            if not isinstance(hypothesis, str):
                raise BenchmarkError("source baseline hypothesis is missing")
            expected_score = wer_record(pair["reference_raw"], hypothesis)
            for field in ("reference_raw", "reference_normalized", "hypothesis_normalized", "wer_counts"):
                if row.get(field) != expected_score[field]:
                    raise BenchmarkError(f"source baseline {field} mismatch")
            artifact = row.get("artifact")
            if not self.fake:
                if not isinstance(artifact, str):
                    raise BenchmarkError("source baseline artifact is missing")
                artifact_path = Path(artifact)
                if not artifact_path.is_dir() or not (artifact_path / "runtime.json").is_file():
                    raise BenchmarkError("source baseline artifact is incomplete")

            imported = json.loads(json.dumps(row))
            imported["reused_from"] = {
                "run_dir": str(self.source_root),
                "run_fingerprint": self.source_manifest["run_fingerprint"],
                "detail": str(detail_path),
                "original_completed_at": row.get("completed_at"),
            }
            imported["baseline_component_fingerprints"] = {
                "source_image": self.source_components,
                "current_image": self.current_provenance["baseline_component_fingerprints"],
            }
            return imported
        except (BenchmarkError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self._reject(key, str(error))
            return None


def endpoint_attribution(endpoints: list[float], gaps: list[dict[str, float]],
                         tolerance: float = 0.05) -> dict[str, Any]:
    unused = set(range(len(endpoints)))
    matches = []
    for gap_index, gap in enumerate(gaps):
        candidates = [index for index in unused if
                      gap["start_seconds"] - tolerance <= endpoints[index] <= gap["end_seconds"] + tolerance]
        if candidates:
            chosen = min(candidates, key=lambda index: abs(
                endpoints[index] - gap["start_seconds"] - ENDPOINT_SILENCE_SECONDS
            ))
            unused.remove(chosen)
            matches.append({"gap_index": gap_index, "endpoint_seconds": endpoints[chosen], "hit": True})
        else:
            matches.append({"gap_index": gap_index, "endpoint_seconds": None, "hit": False})
    hits = sum(item["hit"] for item in matches)
    return {
        "hits": hits, "gaps": len(gaps), "recall": hits / len(gaps) if gaps else None,
        "matches": matches, "unattributed_endpoints": [endpoints[index] for index in sorted(unused)],
    }


def pilot_endpoint_diagnostic(pair: dict[str, Any], detail: dict[str, Any],
                              tolerance: float = 0.05) -> dict[str, Any]:
    endpoints = [float(value) for value in detail.get("endpoints_seconds", [])]
    attribution = endpoint_attribution(endpoints, [pair["boundaries"]["gap"]], tolerance)
    eof = float(pair["duration_seconds"])
    extras = [value for value in attribution["unattributed_endpoints"]
              if abs(value - eof) > tolerance]
    eof_hits = sum(abs(value - eof) <= tolerance for value in attribution["unattributed_endpoints"])
    return {
        "gap_endpointed": attribution["hits"] == 1,
        "eof_endpointed": eof_hits == 1,
        "extra_endpoints_seconds": extras,
        "endpoint_attribution": attribution,
    }


def classify_gap_endpoint(pair: dict[str, Any], detail: dict[str, Any],
                          tolerance: float = 0.05) -> dict[str, Any]:
    """Classify the pair's inserted gap on the runtime's logical endpoint clock."""
    gap = pair["boundaries"]["gap"]
    expected = float(gap["start_seconds"]) + ENDPOINT_SILENCE_SECONDS
    natural = [row for row in detail.get("endpoint_diagnostics", [])
               if row.get("automatic_endpoint") is True and
               row.get("logical_threshold_crossing_seconds") is not None]
    if not natural:
        return {
            "classification": "no_natural_endpoint", "gap": gap,
            "expected_threshold_crossing_seconds": expected,
            "selected_endpoint_diagnostics": None, "natural_endpoint_count": 0,
        }
    selected = min(
        natural,
        key=lambda row: abs(float(row["logical_threshold_crossing_seconds"]) - expected),
    )
    logical = float(selected["logical_threshold_crossing_seconds"])
    delivery = float(selected["event_delivery_position_seconds"])
    if logical < float(gap["start_seconds"]) - tolerance:
        classification = "logical_early"
    elif logical > float(gap["end_seconds"]) + tolerance:
        classification = "logical_late"
    elif (float(gap["start_seconds"]) - tolerance <= delivery <=
          float(gap["end_seconds"]) + tolerance):
        classification = "logical_and_delivery_in_gap"
    elif delivery > float(gap["end_seconds"]) + tolerance:
        classification = "logical_in_gap_delivery_late"
    else:
        raise BenchmarkError("logical endpoint delivery preceded its inserted gap")
    return {
        "classification": classification, "gap": gap,
        "expected_threshold_crossing_seconds": expected,
        "selected_endpoint_diagnostics": selected,
        "natural_endpoint_count": len(natural),
    }


def recommend_endpoint_repair(diagnostics: list[dict[str, Any]]) -> dict[str, str]:
    missed = [row for row in diagnostics if not row["gap_endpointed"]]
    if not missed:
        return {
            "repair": "none",
            "reason": "every inserted gap received a naturally delivered endpoint",
        }
    if all(row["classification"] == "logical_in_gap_delivery_late" for row in missed):
        return {
            "repair": "boundary_timestamp_and_buffer_attribution",
            "reason": "every missed endpoint crossed logically inside its inserted gap",
        }
    return {
        "repair": "vad_driven_endpointing",
        "reason": "at least one missed endpoint crossed outside its gap or never fired",
    }


def boundary_diagnostic(pair: dict[str, Any], hypothesis: str) -> dict[str, Any]:
    first = normalize(pair["members"][0]["reference"]).split()
    second = normalize(pair["members"][1]["reference"]).split()
    reference = first + second
    observed = normalize(hypothesis).split()
    rows, columns = len(reference) + 1, len(observed) + 1
    table = [[0] * columns for _ in range(rows)]
    for i in range(rows): table[i][0] = i
    for j in range(columns): table[0][j] = j
    for i in range(1, rows):
        for j in range(1, columns):
            table[i][j] = min(table[i - 1][j] + 1, table[i][j - 1] + 1,
                              table[i - 1][j - 1] + (reference[i - 1] != observed[j - 1]))
    i, j = len(reference), len(observed)
    deletions: list[tuple[int, str]] = []
    insertions: list[str] = []
    while i or j:
        if i and j and table[i][j] == table[i - 1][j - 1] + (reference[i - 1] != observed[j - 1]):
            i -= 1; j -= 1
        elif i and table[i][j] == table[i - 1][j] + 1:
            i -= 1; deletions.append((i, reference[i]))
        else:
            j -= 1; insertions.append(observed[j])
    boundary_indexes = set(range(max(0, len(first) - 5), min(len(reference), len(first) + 5)))
    vocabulary = {reference[index] for index in boundary_indexes}
    return {
        "first_tail": first[-5:], "second_head": second[:5],
        "boundary_omissions": [word for index, word in reversed(deletions) if index in boundary_indexes],
        "possible_boundary_repetitions": [word for word in reversed(insertions) if word in vocabulary],
    }


def run_process(command: list[str], timeout: float, env: dict[str, str],
                cidfile: Path | None = None, engine: str | None = None) -> tuple[int, str, str, float]:
    if timeout <= 0:
        raise ModeTimeout("mode budget was exhausted before launch")
    started = time.monotonic()
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        if cidfile is not None and engine is not None:
            with contextlib.suppress(OSError):
                container_id = cidfile.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
                    subprocess.run([engine, "rm", "--force", container_id], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise ModeTimeout(f"mode exceeded {timeout:.3f} seconds")
    return process.returncode, stdout, stderr, time.monotonic() - started


class RealBackend:
    def __init__(self, root: Path, run_root: Path, cache_root: Path, models_root: Path,
                 records: dict[str, dict[str, str]], prov: dict[str, Any], engine: str,
                 env: dict[str, str]):
        self.root, self.run_root, self.cache_root = root, run_root, cache_root
        self.models_root, self.records, self.prov = models_root, records, prov
        self.engine, self.env = engine, env

    def cascade(self, source: dict[str, Any], timeout: float, paced: bool = False) -> dict[str, Any]:
        label = source.get("pair_id", "paced-stream")
        bundle = self.run_root / "artifacts" / label / ("paced.cascade" if paced else "cascade.bundle")
        bundle.parent.mkdir(parents=True, exist_ok=True)
        command = [str(self.root / "scripts/cascade"), "--output", str(bundle)]
        if paced: command.append("--pace")
        command.append(source["audio_path"])
        if bundle.exists():
            result = read_json(bundle / "result.json")
            if result.get("source", {}).get("sha256") != source["audio_sha256"]:
                raise BenchmarkError(f"existing cascade artifact has the wrong source: {bundle}")
            stdout, stderr, wall, code = "", "", result.get("timing", {}).get("process", {}).get("wall_seconds"), 0
        else:
            code, stdout, stderr, wall = run_process(
                command, timeout, {**self.env, "NATIVE_ASR_MODEL_VERIFIED_ALIASES":
                                   ",".join((NEMOTRON_ALIAS, PARAKEET_ALIAS))}
            )
            if not (bundle / "result.json").is_file():
                raise BenchmarkError(f"cascade produced no audit bundle (exit {code}): {stderr[-2000:]}")
            result = read_json(bundle / "result.json")
        events = [json.loads(line) for line in (bundle / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        segments = read_json(bundle / "segments.json")
        failure = result.get("failure")
        status = result.get("status")
        if code != 0 or status != "complete":
            raise BenchmarkError(f"cascade failed for {label}: {failure or stderr[-2000:]}")
        if result.get("source", {}).get("sha256") != source["audio_sha256"]:
            raise BenchmarkError(f"cascade source provenance mismatch for {label}")
        if (result.get("provenance", {}).get("git_revision") != self.prov["git_revision"] or
                result.get("provenance", {}).get("adapter_sha256") !=
                self.prov["cascade_adapter_sha256"] or
                result.get("container", {}).get("image_id") != self.prov["image_id"]):
            raise BenchmarkError(f"cascade Git/image/adapter provenance mismatch for {label}")
        aliases = {row.get("alias"): row for row in result.get("models", [])}
        for alias in (NEMOTRON_ALIAS, PARAKEET_ALIAS):
            if aliases.get(alias, {}).get("artifact", {}).get("sha256") != self.records[alias]["sha256"]:
                raise BenchmarkError(f"cascade model provenance mismatch for {label}: {alias}")
        counts = result.get("counts")
        process_timing = result.get("timing", {}).get("process")
        native_timing = result.get("timing", {}).get("native")
        required_counts = ("segments", "provisional_updates", "parakeet_segments",
                           "nemotron_fallbacks", "silence_segments", "warnings")
        if not isinstance(counts, dict) or any(not isinstance(counts.get(key), int) for key in required_counts):
            raise BenchmarkError(f"cascade metrics are missing for {label}")
        if not isinstance(process_timing, dict) or process_timing.get("peak_rss_kb") is None or not isinstance(native_timing, dict):
            raise BenchmarkError(f"cascade timing/RSS metrics are missing for {label}")
        loads = {row["alias"]: row.get("load_count") for row in result["models"]}
        transcript = result.get("text")
        if not isinstance(transcript, str):
            raise BenchmarkError(f"cascade transcript is missing for {label}")
        provisional = [event for event in events if event.get("event") == "transcript_update" and
                       event.get("track_id") == "nemotron" and event.get("state") == "provisional"]
        finals = [event for event in events if event.get("event") == "transcript_update" and
                  event.get("track_id") == "authoritative" and event.get("state") == "cascade_final"]
        correction = []
        nemotron_final = {event["segment_id"]: event for event in events if
                          event.get("event") == "transcript_update" and event.get("track_id") == "nemotron" and
                          event.get("state") == "segment_final"}
        for event in finals:
            prior = nemotron_final.get(event["segment_id"])
            if prior:
                correction.append(event["emitted_monotonic_seconds"] - prior["emitted_monotonic_seconds"])
        detail = {
            "status": "complete", "mode": "cascade_paced" if paced else "cascade",
            "hypothesis_raw": transcript, "hypothesis_normalized": normalize(transcript),
            "timing": result["timing"], "peak_rss_kb": process_timing["peak_rss_kb"],
            "counts": counts, "model_load_counts": loads,
            "segments": len(segments),
            "endpoints_seconds": [float(event["source_time"]["end_seconds"]) for event in finals],
            "endpoint_diagnostics": [
                {
                    "segment_id": event["segment_id"],
                    "source_time": event["source_time"],
                    **event["endpoint_diagnostics"],
                }
                for event in finals
            ],
            "provisional_updates": len(provisional),
            "partial_source_clock_lags_seconds": [
                float(event["emitted_monotonic_seconds"]) - float(event["audio_position_seconds"])
                for event in provisional
            ],
            "correction_latencies_seconds": correction,
            "provenance": {"git_revision": result["provenance"]["git_revision"],
                           "adapter_sha256": result["provenance"]["adapter_sha256"],
                           "image_id": result["container"]["image_id"],
                           "models": result["models"]},
            "artifact": str(bundle), "failure": None,
        }
        if not paced:
            detail.update(wer_record(source["reference_raw"], transcript))
        return detail

    def baseline(self, pair: dict[str, Any], alias: str, timeout: float) -> dict[str, Any]:
        mode = "parakeet" if alias == PARAKEET_ALIAS else "nemotron"
        artifact = self.run_root / "artifacts" / pair["pair_id"] / mode
        artifact.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"baseline-{mode}.", dir=self.cache_root))
        input_dir, output_dir = work / "input", work / "output"
        input_dir.mkdir(); output_dir.mkdir()
        cidfile = work / "container.cid"
        try:
            audio = input_dir / f"{pair['pair_id']}.wav"
            try: os.link(pair["audio_path"], audio)
            except OSError: shutil.copy2(pair["audio_path"], audio)
            user = "65532:65532" if os.getuid() == 0 else f"{os.getuid()}:{os.getgid()}"
            model = "/models/" + self.records[alias]["destination"].split("/", 1)[1]
            arguments = ["transcribe", "/audio", "--model", model, "--device", "cpu",
                         "--format", "json", "--output-dir", "/output", "--word-times",
                         "--concurrency", "1", "--force"]
            if alias == NEMOTRON_ALIAS: arguments.append("--stream")
            command = [self.engine, "run", "--rm", "--network", "none", "--read-only",
                       "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,mode=1777", "--user", user,
                       "--cidfile", str(cidfile),
                       "--mount", f"type=bind,source={(self.models_root / 'nemo-speech').resolve()},target=/models,readonly",
                       "--mount", f"type=bind,source={input_dir.resolve()},target=/audio,readonly",
                       "--mount", f"type=bind,source={output_dir.resolve()},target=/output",
                       "--entrypoint", "/usr/bin/time", IMAGE, "-f",
                       "NATIVE_ASR_TIME\\t%U\\t%S\\t%M\\t%x", "/opt/native-asr/bin/nemo-speech", *arguments]
            code, stdout, stderr, wall = run_process(command, timeout, self.env, cidfile, self.engine)
            outputs = list(output_dir.rglob("*.json"))
            atomic_bytes(artifact / "runtime.stdout.log", stdout.encode(errors="replace"))
            atomic_bytes(artifact / "runtime.stderr.log", stderr.encode(errors="replace"))
            if len(outputs) == 1:
                atomic_bytes(artifact / "runtime.json", outputs[0].read_bytes())
            if code or len(outputs) != 1:
                raise BenchmarkError(f"{mode} baseline failed for {pair['pair_id']} (exit {code}): {stderr[-2000:]}")
            payload = _runtime_json_payload(outputs[0])
            hypothesis = _json_text(payload).strip()
            timing_matches = list(TIME_RE.finditer(stderr))
            if not timing_matches:
                raise BenchmarkError(f"{mode} baseline emitted no timing metrics for {pair['pair_id']}")
            try:
                user_seconds, system_seconds, rss, timed_exit = timing_matches[-1].groups()
                timing = {"wall_seconds": wall, "user_seconds": float(user_seconds),
                          "system_seconds": float(system_seconds), "peak_rss_kb": int(rss),
                          "exit_status": int(timed_exit)}
            except ValueError as error:
                raise BenchmarkError(f"{mode} baseline timing is malformed") from error
            if timing["exit_status"] != code:
                raise BenchmarkError(f"{mode} baseline timing exit mismatch")
            return {
                "status": "complete", "mode": mode, **wer_record(pair["reference_raw"], hypothesis),
                "timing": timing, "peak_rss_kb": timing["peak_rss_kb"], "model_load_count": 1,
                "provenance": {"git_revision": self.prov["git_revision"],
                               "adapter_sha256": self.prov["adapter_sha256"],
                               "image_id": self.prov["image_id"], "model": self.prov["models"][alias]},
                "artifact": str(artifact), "failure": None,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)


class FakeBackend:
    """Deterministic in-process backend, enabled only by the test-only CLI switch."""

    def __init__(self, scenario: str, prov: dict[str, Any]):
        self.scenario, self.prov, self.calls = scenario, prov, 0

    def cascade(self, source: dict[str, Any], timeout: float, paced: bool = False) -> dict[str, Any]:
        self.calls += 1
        if self.scenario == "pilot-fatal" and self.calls == 1:
            raise BenchmarkError("forced pilot runtime failure")
        if self.scenario == "timeout" and self.calls == 1:
            raise ModeTimeout(f"mode exceeded {timeout:.3f} seconds")
        if self.scenario == "deadline":
            raise DeadlineExceeded("forced overall deadline")
        reference = source["reference_raw"]
        if paced:
            gaps = source["gaps"]
            endpoints = [gap["start_seconds"] + ENDPOINT_SILENCE_SECONDS
                         for gap in gaps] + [source["duration_seconds"]]
            segments = len(gaps) + 1
        else:
            gap = source["boundaries"]["gap"]
            endpoints = [gap["start_seconds"] + ENDPOINT_SILENCE_SECONDS,
                         source["duration_seconds"]]
            segments = 2
            if self.scenario == "pilot-missed-endpoint":
                endpoints, segments = [source["duration_seconds"]], 1
            elif self.scenario == "pilot-extra-endpoints":
                endpoints = [source["boundaries"]["first"]["end_seconds"] / 2,
                             *endpoints]
                segments = 3
        endpoint_diagnostics = []
        for index, endpoint in enumerate(endpoints):
            automatic = index < len(endpoints) - 1
            crossing = endpoint - 0.1 if automatic else None
            endpoint_diagnostics.append({
                "segment_id": f"segment-{index + 1:06d}",
                "source_time": {"start_seconds": 0.0, "end_seconds": endpoint},
                "schema_version": 1, "automatic_endpoint": automatic,
                "decoder_clock_seconds": endpoint - 0.05 if automatic else None,
                "last_token_seconds": crossing - ENDPOINT_SILENCE_SECONDS if automatic else None,
                "logical_threshold_crossing_seconds": crossing,
                "raw_delivery_frontier_seconds": endpoint,
                "event_delivery_position_seconds": endpoint,
                "delivery_lag_seconds": endpoint - crossing if automatic else None,
            })
        loads = {NEMOTRON_ALIAS: 1, PARAKEET_ALIAS: 1}
        if self.scenario == "load-count" and self.calls == 1: loads[PARAKEET_ALIAS] = 2
        counts = {"segments": segments, "provisional_updates": segments,
                  "parakeet_segments": segments, "nemotron_fallbacks": 0,
                  "silence_segments": 0, "warnings": 0}
        if self.scenario == "missing-metrics" and self.calls == 1:
            raise BenchmarkError("cascade metrics are missing")
        detail = {
            "status": "complete", "mode": "cascade_paced" if paced else "cascade",
            "hypothesis_raw": reference, "hypothesis_normalized": normalize(reference),
            "timing": {"process": {"wall_seconds": source["duration_seconds"] if paced else 0.1,
                                      "peak_rss_kb": 1000}, "native": {"inference_seconds": 0.05}},
            "peak_rss_kb": 1000, "counts": counts, "model_load_counts": loads,
            "segments": segments, "endpoints_seconds": endpoints,
            "endpoint_diagnostics": endpoint_diagnostics,
            "provisional_updates": segments, "partial_source_clock_lags_seconds": [0.25] * segments,
            "correction_latencies_seconds": [0.1] * segments, "provenance": self.prov,
            "artifact": "fake", "failure": None,
        }
        if not paced: detail.update(wer_record(reference, reference))
        return detail

    def baseline(self, pair: dict[str, Any], alias: str, timeout: float) -> dict[str, Any]:
        mode = "parakeet" if alias == PARAKEET_ALIAS else "nemotron"
        return {
            "status": "complete", "mode": mode, **wer_record(pair["reference_raw"], pair["reference_raw"]),
            "timing": {"wall_seconds": 0.05, "user_seconds": 0.01, "system_seconds": 0.01,
                       "peak_rss_kb": 1000, "exit_status": 0},
            "peak_rss_kb": 1000, "model_load_count": 1,
            "provenance": {"git_revision": self.prov["git_revision"],
                           "adapter_sha256": self.prov["adapter_sha256"],
                           "image_id": self.prov["image_id"],
                           "model": self.prov["models"][alias]},
            "artifact": "fake", "failure": None,
        }


def aggregate_pairs(pairs: list[dict[str, Any]], details: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    output: dict[str, Any] = {"splits": {}, "pooled": {}}
    for split in ("test-clean", "test-other", "pooled"):
        chosen = pairs if split == "pooled" else [pair for pair in pairs if pair["split"] == split]
        summary: dict[str, Any] = {"pairs": len(chosen)}
        for mode in MODES:
            completed = [details[pair["pair_id"]][mode] for pair in chosen
                         if details.get(pair["pair_id"], {}).get(mode, {}).get("status") == "complete"]
            counts = _sum_counts(completed)
            summary[mode] = {"completed": len(completed), "wer_counts": counts,
                             "wer": _wer(counts) if len(completed) == len(chosen) else None}
        cascades = [details[pair["pair_id"]]["cascade"] for pair in chosen
                    if details.get(pair["pair_id"], {}).get("cascade", {}).get("status") == "complete"]
        summary["cascade_contract"] = {
            "with_provisional_update": sum(row["counts"]["provisional_updates"] > 0 for row in cascades),
            "with_two_authoritative_segments": sum(row["counts"]["segments"] >= 2 for row in cascades),
            "authoritative_segments": sum(row["counts"]["segments"] for row in cascades),
            "nonempty_authoritative_segments": sum(
                row["counts"]["segments"] - row["counts"]["silence_segments"] for row in cascades),
            "fallbacks": sum(row["counts"]["nemotron_fallbacks"] for row in cascades),
            "silence_segments": sum(row["counts"]["silence_segments"] for row in cascades),
        }
        if split == "pooled":
            output["pooled"] = summary
        else:
            output["splits"][split] = summary
    return output


def pair_gate(pairs: list[dict[str, Any]], details: dict[str, dict[str, dict[str, Any]]],
              aggregate: dict[str, Any]) -> dict[str, Any]:
    total = len(pairs)
    pooled = aggregate["pooled"]
    contract = pooled["cascade_contract"]
    minimum = math.ceil(total * 0.95)
    nonempty = contract["nonempty_authoritative_segments"]
    fallback_rate = contract["fallbacks"] / nonempty if nonempty else None
    all_complete = all(details.get(pair["pair_id"], {}).get(mode, {}).get("status") == "complete"
                       for pair in pairs for mode in MODES)
    loads_ok = all(
        details.get(pair["pair_id"], {}).get("cascade", {}).get("model_load_counts") ==
        {NEMOTRON_ALIAS: 1, PARAKEET_ALIAS: 1}
        for pair in pairs
    ) and all(details.get(pair["pair_id"], {}).get(mode, {}).get("model_load_count") == 1
              for pair in pairs for mode in ("parakeet", "nemotron"))
    checks: dict[str, dict[str, Any]] = {}
    def check(name: str, passed: bool, actual: Any, requirement: str) -> None:
        checks[name] = {"pass": bool(passed), "actual": actual, "requirement": requirement}
    check("all_modes_complete", all_complete, all_complete, f"all {total} pairs x 3 modes complete")
    check("one_load_per_model", loads_ok, loads_ok, "exactly one load in every mode")
    check("provisional_pair_rate", contract["with_provisional_update"] >= minimum,
          contract["with_provisional_update"] / total if total else None, ">= 0.95")
    check("two_segment_pair_rate", contract["with_two_authoritative_segments"] >= minimum,
          contract["with_two_authoritative_segments"] / total if total else None, ">= 0.95")
    check("no_silence_segments", contract["silence_segments"] == 0,
          contract["silence_segments"], "== 0")
    check("fallback_rate", fallback_rate is not None and fallback_rate <= 0.01,
          fallback_rate, "<= 0.01 of nonempty authoritative segments")
    for split in ("test-clean", "test-other"):
        item = aggregate["splits"][split]
        cascade_wer, parakeet_wer, nemotron_wer = (item[key]["wer"] for key in MODES)
        check(f"{split}_cascade_vs_parakeet",
              cascade_wer is not None and parakeet_wer is not None and cascade_wer - parakeet_wer <= 0.015,
              None if cascade_wer is None or parakeet_wer is None else cascade_wer - parakeet_wer,
              "<= 0.015 absolute WER")
        check(f"{split}_cascade_vs_nemotron",
              cascade_wer is not None and nemotron_wer is not None and cascade_wer - nemotron_wer <= 0.005,
              None if cascade_wer is None or nemotron_wer is None else cascade_wer - nemotron_wer,
              "<= 0.005 absolute WER")
    cascade_wer, parakeet_wer = pooled["cascade"]["wer"], pooled["parakeet"]["wer"]
    check("pooled_cascade_vs_parakeet",
          cascade_wer is not None and parakeet_wer is not None and cascade_wer - parakeet_wer <= 0.01,
          None if cascade_wer is None or parakeet_wer is None else cascade_wer - parakeet_wer,
          "<= 0.01 absolute WER")
    return {"pass": all(row["pass"] for row in checks.values()), "checks": checks}


def stream_gate(stream: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    endpoints = endpoint_attribution(detail.get("endpoints_seconds", []), stream["gaps"])
    counts = detail.get("counts", {})
    nonempty = counts.get("segments", 0) - counts.get("silence_segments", 0)
    fallback_rate = counts.get("nemotron_fallbacks", 0) / nonempty if nonempty else None
    partial_p95 = percentile(detail.get("partial_source_clock_lags_seconds", []), 0.95)
    correction_p95 = percentile(detail.get("correction_latencies_seconds", []), 0.95)
    process = detail.get("timing", {}).get("process", {})
    rtf = process.get("wall_seconds") / stream["duration_seconds"] if process.get("wall_seconds") is not None else None
    checks: dict[str, dict[str, Any]] = {}
    def check(name: str, passed: bool, actual: Any, requirement: str) -> None:
        checks[name] = {"pass": bool(passed), "actual": actual, "requirement": requirement}
    check("complete_valid_stream", detail.get("status") == "complete", detail.get("status"), "complete")
    check("genuine_partials", detail.get("provisional_updates", 0) > 0,
          detail.get("provisional_updates"), "> 0")
    check("one_load_per_model", detail.get("model_load_counts") ==
          {NEMOTRON_ALIAS: 1, PARAKEET_ALIAS: 1}, detail.get("model_load_counts"), "exactly one each")
    check("endpoint_recall", endpoints["recall"] is not None and endpoints["recall"] >= 0.95,
          endpoints["recall"], ">= 0.95")
    check("fallback_rate", fallback_rate is not None and fallback_rate <= 0.01, fallback_rate, "<= 0.01")
    check("partial_source_clock_lag_p95", partial_p95 is not None and partial_p95 <= 2.0,
          partial_p95, "<= 2.0 seconds")
    check("correction_latency_p95", correction_p95 is not None and correction_p95 <= 2.5,
          correction_p95, "<= 2.5 seconds")
    check("paced_end_to_end_rtf", rtf is not None and rtf <= 1.10, rtf, "<= 1.10")
    return {"pass": all(row["pass"] for row in checks.values()), "checks": checks,
            "endpoint_attribution": endpoints}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="scripts/benchmark-cascade-bounded",
                                     description="Run only the bounded two-pass cascade gate.")
    result.add_argument("--limit-per-split", type=int, default=100)
    result.add_argument("--pilot-pairs-per-split", type=int, default=5)
    result.add_argument("--pair-timeout-seconds", type=float, default=120)
    result.add_argument("--paced-timeout-seconds", type=float, default=900)
    result.add_argument("--overall-deadline-seconds", type=float, default=3600)
    result.add_argument("--stream-max-seconds", type=float, default=300)
    result.add_argument("--results-root", type=Path,
                        default=Path("/data/benchmarks/native-asr/cascade"))
    result.add_argument("--cache-root", type=Path)
    result.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--test-fake-scenario", choices=("pass", "pilot-fatal", "pilot-missed-endpoint",
                                                          "pilot-extra-endpoints", "timeout", "deadline",
                                                          "missing-metrics", "load-count"),
                        help=argparse.SUPPRESS)
    result.add_argument("--reuse-baselines-from", type=Path, metavar="RUN_DIR",
                        help="reuse individually verified Parakeet/Nemotron baseline details")
    result.add_argument(
        "--endpoint-diagnostics-only", action="store_true",
        help="run only the fixed ten-pair endpoint-clock diagnostic pilot",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    if args.limit_per_split <= 0 or args.limit_per_split > 100 or args.limit_per_split % 2:
        parser().error("--limit-per-split must be a positive even integer no greater than 100")
    if args.pilot_pairs_per_split <= 0 or args.pilot_pairs_per_split > args.limit_per_split // 2:
        parser().error("--pilot-pairs-per-split must fit within the selected pairs")
    if args.endpoint_diagnostics_only and args.pilot_pairs_per_split != 5:
        parser().error("--endpoint-diagnostics-only requires exactly five pilot pairs per split")
    if args.endpoint_diagnostics_only and args.reuse_baselines_from is not None:
        parser().error("--endpoint-diagnostics-only does not run or reuse baselines")
    if min(args.pair_timeout_seconds, args.paced_timeout_seconds,
           args.overall_deadline_seconds, args.stream_max_seconds) <= 0:
        parser().error("timeouts and duration caps must be positive")
    fake = args.test_fake_scenario is not None
    if fake and os.environ.get("NATIVE_ASR_ALLOW_TEST_BACKEND") != "1":
        parser().error("the fake backend requires NATIVE_ASR_ALLOW_TEST_BACKEND=1")
    started = time.monotonic()
    prior_elapsed = 0.0
    root = Path(__file__).resolve().parents[2]
    datasets_root = Path(os.environ.get("NATIVE_ASR_DATASETS", "/data/datasets/native-asr"))
    models_root = Path(os.environ.get("NATIVE_ASR_MODELS", "/data/models"))
    native_cache = Path(os.environ.get("NATIVE_ASR_CACHE", "/data/cache/native-asr"))
    cache_root = (args.cache_root or native_cache / "cascade-bounded").resolve()
    manifest_path = Path(os.environ.get("NATIVE_ASR_MODEL_MANIFEST", root / "manifests/models.lock"))
    engine = os.environ.get("NATIVE_ASR_CONTAINER_ENGINE", "docker")

    def remaining() -> float:
        value = args.overall_deadline_seconds - prior_elapsed - (time.monotonic() - started)
        if value <= 0: raise DeadlineExceeded("60-minute overall benchmark deadline expired")
        return value

    try:
        records = model_records(manifest_path)
        prov = provenance(root, records, engine, fake)
        if prov["git_dirty"] and not (args.allow_dirty and fake):
            raise BenchmarkError("benchmark provenance requires a clean Git worktree")
        if not fake:
            subprocess.run([str(root / "scripts/verify-models"), NEMOTRON_ALIAS, PARAKEET_ALIAS],
                           check=True, stdout=subprocess.DEVNULL)
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_manifest, pairs = prepare_pair_cache(datasets_root, cache_root, args.limit_per_split)
        remaining()
        options = {
            "limit_per_split": args.limit_per_split, "pairs_per_split": args.limit_per_split // 2,
            "ranking": "sha256(utterance_id)", "pairing": "adjacent-ranked",
            "gap_seconds": GAP_SECONDS, "gap_frames": GAP_FRAMES,
            "endpointing": ENDPOINT_POLICY,
            "pilot_pairs_per_split": args.pilot_pairs_per_split,
            "endpoint_diagnostics_only": args.endpoint_diagnostics_only,
            "pair_timeout_seconds": args.pair_timeout_seconds,
            "paced_timeout_seconds": args.paced_timeout_seconds,
            "overall_deadline_seconds": args.overall_deadline_seconds,
            "stream_max_seconds_exclusive": args.stream_max_seconds,
            "normalization": NORMALIZATION_VERSION,
        }
        baseline_policy = baseline_options(options)
        reuse = (BaselineReuse(args.reuse_baselines_from, cache_manifest, pairs, prov,
                               baseline_policy, engine, fake)
                 if args.reuse_baselines_from is not None else None)
        run_identity = {"schema_version": SCHEMA_VERSION, "recipe_version": RECIPE_VERSION,
                        "cache_fingerprint": cache_manifest["cache_fingerprint"],
                        "provenance": prov, "options": options,
                        "baseline_options": baseline_policy,
                        "baseline_reuse": reuse.identity() if reuse is not None else None}
        run_fingerprint = fingerprint(run_identity)
        run_root = args.results_root.resolve() / run_fingerprint
        run_root.mkdir(parents=True, exist_ok=True)
        os.chmod(run_root, 0o700)
        lock_handle = (run_root / "run.lock").open("a+")
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BenchmarkError(f"an identical bounded run is already active: {run_root}") from error
        run_manifest = {"run_fingerprint": run_fingerprint, "created_at": utc_now(),
                        "identity": run_identity, "pair_cache_manifest":
                        str((cache_root / cache_manifest["cache_fingerprint"] / "manifest.json").resolve()),
                        "results_root": str(run_root)}
        existing_manifest = run_root / "run_manifest.json"
        if existing_manifest.is_file() and read_json(existing_manifest).get("identity") != run_identity:
            raise BenchmarkError(f"run manifest fingerprint mismatch: {existing_manifest}")
        if not existing_manifest.exists(): atomic_json(existing_manifest, run_manifest)
        if reuse is not None:
            reuse.attach(run_root)
        final_path = run_root / "verdict.json"
        if final_path.is_file() and (
                read_json(final_path).get("phase") == "complete" or
                (read_json(final_path).get("phase") == "endpoint_diagnostics" and
                 read_json(final_path).get("diagnostic_collection_success") is True)):
            verdict = read_json(final_path)
            print(f"resume: bounded run already complete: {run_root}")
            return 0 if verdict.get("pass") else 1
        state_path = run_root / "state.json"
        state = read_json(state_path) if state_path.is_file() else {
            "run_fingerprint": run_fingerprint, "phase": "pilot", "completed_modes": [],
            "completed_pairs": [], "failures": [], "elapsed_seconds": 0.0,
            "updated_at": utc_now(),
        }
        if state.get("run_fingerprint") != run_fingerprint:
            raise BenchmarkError("resume state fingerprint mismatch")
        prior_elapsed = float(state.get("elapsed_seconds", 0.0))
        remaining()
        backend: RealBackend | FakeBackend = (FakeBackend(args.test_fake_scenario or "pass", prov) if fake else
            RealBackend(root, run_root, cache_root, models_root, records, prov, engine, dict(os.environ)))
        details: dict[str, dict[str, dict[str, Any]]] = {}
        for pair in pairs:
            details[pair["pair_id"]] = {}
            for mode in MODES:
                path = run_root / "details" / pair["split"] / pair["pair_id"] / f"{mode}.json"
                if path.is_file():
                    row = read_json(path)
                    if row.get("run_fingerprint") != run_fingerprint or row.get("pair_fingerprint") != pair["pair_fingerprint"]:
                        raise BenchmarkError(f"resume detail fingerprint mismatch: {path}")
                    details[pair["pair_id"]][mode] = row
                    if reuse is not None and "reused_from" in row:
                        reuse.mark_imported(f"{pair['pair_id']}:{mode}",
                                            row["reused_from"]["detail"])

        def checkpoint() -> None:
            state["elapsed_seconds"] = prior_elapsed + (time.monotonic() - started)
            state["updated_at"] = utc_now(); atomic_json(state_path, state)

        def execute_mode(pair: dict[str, Any], mode: str) -> dict[str, Any]:
            if mode in details[pair["pair_id"]]: return details[pair["pair_id"]][mode]
            used = sum(float(details[pair["pair_id"]].get(item, {}).get("budget_seconds", 0) or 0)
                       for item in MODES
                       if "reused_from" not in details[pair["pair_id"]].get(item, {}))
            try:
                pair_remaining = args.pair_timeout_seconds - used
                if pair_remaining <= 0:
                    raise ModeTimeout("cumulative pair budget was exhausted before launch")
                timeout = min(remaining(), pair_remaining)
                imported = (reuse.import_detail(pair, mode)
                            if reuse is not None and mode in BASELINE_MODES else None)
                if imported is not None:
                    row = imported
                else:
                    if mode == "cascade": row = backend.cascade(pair, timeout)
                    elif mode == "parakeet": row = backend.baseline(pair, PARAKEET_ALIAS, timeout)
                    else: row = backend.baseline(pair, NEMOTRON_ALIAS, timeout)
                    timing = row.get("timing", {})
                    row["budget_seconds"] = float(
                        timing.get("process", {}).get("wall_seconds", 0)
                        if mode == "cascade" else timing.get("wall_seconds", 0)
                    )
            except DeadlineExceeded:
                raise
            except BenchmarkError as error:
                row = {"status": "failed", "mode": mode, "failure": {"kind": type(error).__name__,
                       "message": str(error)}, "timing": {"wall_seconds": 0.0},
                       "budget_seconds": timeout if isinstance(error, ModeTimeout) and
                       "timeout" in locals() else 0.0}
                artifact = run_root / "artifacts" / pair["pair_id"] / (
                    "cascade.bundle" if mode == "cascade" else mode)
                if artifact.exists(): row["artifact"] = str(artifact)
            row.update({"schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                        "pair_fingerprint": pair["pair_fingerprint"], "pair_id": pair["pair_id"],
                        "split": pair["split"], "completed_at": utc_now()})
            path = run_root / "details" / pair["split"] / pair["pair_id"] / f"{mode}.json"
            atomic_json(path, row)
            if reuse is not None and "reused_from" in row:
                reuse.mark_imported(f"{pair['pair_id']}:{mode}", row["reused_from"]["detail"])
            details[pair["pair_id"]][mode] = row
            key = f"{pair['pair_id']}:{mode}"
            if key not in state["completed_modes"]: state["completed_modes"].append(key)
            if row["status"] != "complete": state["failures"].append({"key": key, **row["failure"]})
            checkpoint()
            return row

        pilot = [pair for split in ("test-clean", "test-other")
                 for pair in [item for item in pairs if item["split"] == split][:args.pilot_pairs_per_split]]
        pilot_diagnostics = []
        extra_endpoint_pairs = 0
        allowed_extra_endpoint_pairs = math.floor(
            len(pilot) * PILOT_MAX_EXTRA_ENDPOINT_PAIR_RATE
        )
        if args.endpoint_diagnostics_only:
            for pair in pilot:
                row = execute_mode(pair, "cascade")
                pilot_ok = row.get("status") == "complete" and row.get("model_load_counts") == {
                    NEMOTRON_ALIAS: 1, PARAKEET_ALIAS: 1}
                if not pilot_ok:
                    diagnostic_verdict = {
                        "schema_version": SCHEMA_VERSION,
                        "run_fingerprint": run_fingerprint,
                        "phase": "endpoint_diagnostics",
                        "pass": False,
                        "diagnostic_collection_success": False,
                        "endpoint_contract_success": None,
                        "stopped_at_pair": pair["pair_id"],
                        "failure": row.get("failure") or {
                            "kind": "contract", "message": "incorrect model load count"},
                        "endpoint_diagnostics": pilot_diagnostics,
                        "completed_at": utc_now(), "results_root": str(run_root),
                    }
                    atomic_json(run_root / "endpoint_diagnostics.json", diagnostic_verdict)
                    atomic_json(run_root / "pilot_verdict.json", diagnostic_verdict)
                    atomic_json(final_path, diagnostic_verdict)
                    state["phase"] = "endpoint_diagnostics_failed"; checkpoint()
                    print(
                        f"FAIL: endpoint diagnostic stopped at {pair['pair_id']}; evidence: {run_root}",
                        file=sys.stderr,
                    )
                    return 1
                delivery = pilot_endpoint_diagnostic(pair, row)
                clock = classify_gap_endpoint(pair, row)
                diagnostic = {
                    "pair_id": pair["pair_id"], "split": pair["split"],
                    **delivery, **clock,
                    "segment_diagnostics": row["endpoint_diagnostics"],
                }
                pilot_diagnostics.append(diagnostic)
                if diagnostic["extra_endpoints_seconds"]:
                    extra_endpoint_pairs += 1

            hits = sum(row["gap_endpointed"] for row in pilot_diagnostics)
            lags = [
                float(segment["delivery_lag_seconds"])
                for row in pilot_diagnostics for segment in row["segment_diagnostics"]
                if segment.get("automatic_endpoint") is True and
                segment.get("delivery_lag_seconds") is not None
            ]
            endpoint_contract_success = (
                hits == len(pilot) and extra_endpoint_pairs <= allowed_extra_endpoint_pairs
            )
            diagnostic_verdict = {
                "schema_version": SCHEMA_VERSION,
                "run_fingerprint": run_fingerprint,
                "phase": "endpoint_diagnostics",
                "pass": endpoint_contract_success,
                "diagnostic_collection_success": True,
                "endpoint_contract_success": endpoint_contract_success,
                "pairs": len(pilot),
                "inserted_gap_endpoint_recall": hits / len(pilot) if pilot else None,
                "classifications": {
                    category: sum(row["classification"] == category for row in pilot_diagnostics)
                    for category in (
                        "logical_and_delivery_in_gap", "logical_in_gap_delivery_late",
                        "logical_early", "logical_late", "no_natural_endpoint",
                    )
                },
                "delivery_lag_seconds": {
                    "count": len(lags), "minimum": min(lags) if lags else None,
                    "p50": percentile(lags, 0.50), "p95": percentile(lags, 0.95),
                    "maximum": max(lags) if lags else None,
                },
                "extra_endpoint_pairs": extra_endpoint_pairs,
                "allowed_extra_endpoint_pairs": allowed_extra_endpoint_pairs,
                "endpoint_diagnostics": pilot_diagnostics,
                "recommended_next_repair": recommend_endpoint_repair(pilot_diagnostics),
                "completed_at": utc_now(), "results_root": str(run_root),
            }
            if not endpoint_contract_success:
                diagnostic_verdict["failure"] = {
                    "kind": "endpoint_contract",
                    "message": "the fixed ten-pair endpoint contract still fails",
                }
            atomic_json(run_root / "endpoint_diagnostics.json", diagnostic_verdict)
            atomic_json(run_root / "pilot_verdict.json", diagnostic_verdict)
            atomic_json(final_path, diagnostic_verdict)
            state["phase"] = "endpoint_diagnostics_complete"; checkpoint()
            print(
                ("PASS" if endpoint_contract_success else "FAIL") +
                f": endpoint-clock diagnostic; evidence: {run_root}"
            )
            return 0 if endpoint_contract_success else 1

        for pair in pilot:
            row = execute_mode(pair, "cascade")
            pilot_ok = row.get("status") == "complete" and row.get("model_load_counts") == {
                NEMOTRON_ALIAS: 1, PARAKEET_ALIAS: 1}
            if not pilot_ok:
                pilot_verdict = {"schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                    "phase": "pilot", "pass": False, "stopped_at_pair": pair["pair_id"],
                    "failure": row.get("failure") or {"kind": "contract", "message": "incorrect model load count"},
                    "completed_at": utc_now(), "results_root": str(run_root)}
                atomic_json(run_root / "pilot_verdict.json", pilot_verdict)
                atomic_json(final_path, pilot_verdict)
                state["phase"] = "pilot_failed"; checkpoint()
                print(f"FAIL: cascade pilot stopped at {pair['pair_id']}; evidence: {run_root}", file=sys.stderr)
                return 1
            diagnostic = {"pair_id": pair["pair_id"], **pilot_endpoint_diagnostic(pair, row)}
            pilot_diagnostics.append(diagnostic)
            if not diagnostic["gap_endpointed"]:
                pilot_verdict = {"schema_version": SCHEMA_VERSION,
                    "run_fingerprint": run_fingerprint, "phase": "pilot", "pass": False,
                    "stopped_at_pair": pair["pair_id"], "failure": {
                        "kind": "endpoint_contract",
                        "message": "inserted 1.6-second gap was not endpointed",
                    }, "endpoint_diagnostics": pilot_diagnostics,
                    "completed_at": utc_now(), "results_root": str(run_root)}
                atomic_json(run_root / "pilot_verdict.json", pilot_verdict)
                atomic_json(final_path, pilot_verdict)
                state["phase"] = "pilot_failed"; checkpoint()
                print(f"FAIL: cascade pilot missed the gap at {pair['pair_id']}; evidence: {run_root}",
                      file=sys.stderr)
                return 1
            if diagnostic["extra_endpoints_seconds"]:
                extra_endpoint_pairs += 1
            if extra_endpoint_pairs > allowed_extra_endpoint_pairs:
                pilot_verdict = {"schema_version": SCHEMA_VERSION,
                    "run_fingerprint": run_fingerprint, "phase": "pilot", "pass": False,
                    "stopped_at_pair": pair["pair_id"], "failure": {
                        "kind": "over_segmentation",
                        "message": "extra endpoints remain widespread under the 1.2-second policy",
                    }, "extra_endpoint_pairs": extra_endpoint_pairs,
                    "allowed_extra_endpoint_pairs": allowed_extra_endpoint_pairs,
                    "endpoint_diagnostics": pilot_diagnostics,
                    "completed_at": utc_now(), "results_root": str(run_root)}
                atomic_json(run_root / "pilot_verdict.json", pilot_verdict)
                atomic_json(final_path, pilot_verdict)
                state["phase"] = "pilot_failed"; checkpoint()
                print(f"FAIL: cascade pilot remained over-segmented at {pair['pair_id']}; evidence: {run_root}",
                      file=sys.stderr)
                return 1
        atomic_json(run_root / "pilot_verdict.json", {"schema_version": SCHEMA_VERSION,
                    "run_fingerprint": run_fingerprint, "phase": "pilot", "pass": True,
                    "pairs": len(pilot), "extra_endpoint_pairs": extra_endpoint_pairs,
                    "allowed_extra_endpoint_pairs": allowed_extra_endpoint_pairs,
                    "endpoint_diagnostics": pilot_diagnostics, "completed_at": utc_now()})
        state["phase"] = "pairs"; checkpoint()

        for pair in pairs:
            for mode in MODES:
                execute_mode(pair, mode)
            pair_rows = details[pair["pair_id"]]
            pair_summary = {
                "schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                "pair_fingerprint": pair["pair_fingerprint"], "pair_id": pair["pair_id"],
                "split": pair["split"], "source": pair,
                "modes": pair_rows,
                "boundary_diagnostic": boundary_diagnostic(
                    pair, pair_rows.get("cascade", {}).get("hypothesis_raw", "")),
                "complete": all(pair_rows.get(mode, {}).get("status") == "complete" for mode in MODES),
            }
            atomic_json(run_root / "pairs" / pair["split"] / f"{pair['pair_id']}.json", pair_summary)
            if pair["pair_id"] not in state["completed_pairs"]: state["completed_pairs"].append(pair["pair_id"])
            checkpoint()
        aggregate = aggregate_pairs(pairs, details)
        gate = pair_gate(pairs, details, aggregate)
        highest = []
        boundary_issues = []
        for pair in pairs:
            cascade_row = details[pair["pair_id"]].get("cascade", {})
            counts = cascade_row.get("wer_counts")
            if counts and counts["reference_words"]:
                highest.append({"pair_id": pair["pair_id"], "split": pair["split"],
                                "wer": counts["errors"] / counts["reference_words"],
                                "errors": counts["errors"], "reference_words": counts["reference_words"]})
            diagnostic = boundary_diagnostic(pair, cascade_row.get("hypothesis_raw", ""))
            if diagnostic["boundary_omissions"] or diagnostic["possible_boundary_repetitions"]:
                boundary_issues.append({"pair_id": pair["pair_id"], **diagnostic})
        aggregate["highest_error_pairs"] = sorted(highest, key=lambda row: (-row["wer"], row["pair_id"]))[:10]
        aggregate["boundary_issues"] = boundary_issues
        atomic_json(run_root / "summary.json", aggregate)
        atomic_json(run_root / "pair_gate.json", gate)
        if not gate["pass"]:
            verdict = {"schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                       "phase": "pair_gate", "pass": False,
                       "failed_guardrails": [name for name, row in gate["checks"].items() if not row["pass"]],
                       "highest_error_pairs": aggregate["highest_error_pairs"],
                       "boundary_issues": boundary_issues, "completed_at": utc_now(),
                       "results_root": str(run_root), "next_action": "retain evidence; do not start a larger benchmark"}
            atomic_json(final_path, verdict); state["phase"] = "pair_gate_failed"; checkpoint()
            print(f"FAIL: pair gate; evidence: {run_root}", file=sys.stderr)
            return 1

        state["phase"] = "paced_stream"; checkpoint(); remaining()
        stream = prepare_stream(cache_manifest, cache_root, datasets_root, args.stream_max_seconds)
        stream_detail_path = run_root / "paced_stream.json"
        if stream_detail_path.is_file():
            stream_detail = read_json(stream_detail_path)
            if stream_detail.get("run_fingerprint") != run_fingerprint or \
                    stream_detail.get("stream_fingerprint") != stream["stream_fingerprint"]:
                raise BenchmarkError("paced stream resume fingerprint mismatch")
        else:
            timeout = min(remaining(), args.paced_timeout_seconds)
            try:
                stream_detail = backend.cascade(stream, timeout, paced=True)
            except DeadlineExceeded:
                raise
            except BenchmarkError as error:
                stream_detail = {"status": "failed", "failure": {"kind": type(error).__name__,
                                 "message": str(error)}, "counts": {}, "timing": {}}
            stream_detail.update({"schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                                  "stream_fingerprint": stream["stream_fingerprint"],
                                  "stream_manifest": stream, "completed_at": utc_now()})
            atomic_json(stream_detail_path, stream_detail); checkpoint()
        paced_gate = stream_gate(stream, stream_detail)
        atomic_json(run_root / "paced_gate.json", paced_gate)
        passed = paced_gate["pass"]
        verdict = {"schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                   "phase": "complete", "pass": passed, "pair_gate_pass": True,
                   "paced_gate_pass": passed,
                   "failed_guardrails": [name for name, row in paced_gate["checks"].items() if not row["pass"]],
                   "completed_at": utc_now(), "results_root": str(run_root),
                   "next_action": ("ready for the next engineering stage; not approved for default promotion"
                                   if passed else "retain evidence; do not start a larger benchmark")}
        atomic_json(final_path, verdict); state["phase"] = "complete"; checkpoint()
        print(("PASS" if passed else "FAIL") + f": bounded cascade gate; evidence: {run_root}")
        return 0 if passed else 1
    except DeadlineExceeded as error:
        if "run_root" in locals() and "run_fingerprint" in locals():
            verdict = {"schema_version": SCHEMA_VERSION, "run_fingerprint": run_fingerprint,
                       "phase": "deadline", "pass": False,
                       "failed_guardrails": ["overall_deadline"],
                       "failure": {"kind": "DeadlineExceeded", "message": str(error)},
                       "completed_at": utc_now(), "results_root": str(run_root),
                       "next_action": "retain checkpointed evidence; do not start a larger benchmark"}
            with contextlib.suppress(OSError):
                atomic_json(run_root / "verdict.json", verdict)
            if "state" in locals() and "state_path" in locals():
                state["phase"] = "deadline"
                state["elapsed_seconds"] = prior_elapsed + (time.monotonic() - started)
                state["updated_at"] = utc_now()
                with contextlib.suppress(OSError): atomic_json(state_path, state)
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except (BenchmarkError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
