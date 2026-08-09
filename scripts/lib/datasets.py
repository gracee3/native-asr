#!/usr/bin/env python3
"""Locked dataset acquisition, verification, and PCM preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import wave
import zipfile


ROOT = Path(os.environ["NATIVE_ASR_REPO_ROOT"])
DATASETS = Path(os.environ["NATIVE_ASR_DATASETS"])
CACHE = Path(os.environ["NATIVE_ASR_CACHE"])
LOCK = Path(os.environ.get("NATIVE_ASR_DATASET_MANIFEST", ROOT / "manifests/datasets.lock"))


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"error: {message}")


def digest(path: Path, algorithm: str = "sha256") -> str:
    result = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def records() -> list[dict[str, str]]:
    keys = (
        "asset_id", "dataset", "name", "source", "revision", "filename",
        "sha256", "md5", "size", "license", "packaging", "install_path",
        "expected_count", "notes",
    )
    result = []
    for number, raw in enumerate(LOCK.read_text(encoding="utf-8").splitlines(), 1):
        if not raw or raw.startswith("#"):
            continue
        fields = raw.split("|")
        if len(fields) != len(keys):
            fail(f"{LOCK}:{number}: expected {len(keys)} fields, got {len(fields)}")
        result.append(dict(zip(keys, fields, strict=True)))
    return result


def grouped() -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for row in records():
        result.setdefault(row["dataset"], []).append(row)
    return result


def selected(names: list[str]) -> dict[str, list[dict[str, str]]]:
    catalog = grouped()
    if not names:
        return catalog
    unknown = sorted(set(names) - catalog.keys())
    if unknown:
        fail(f"unknown dataset: {', '.join(unknown)}")
    return {name: catalog[name] for name in names}


def archive_path(row: dict[str, str]) -> Path:
    return CACHE / "datasets" / "archives" / row["filename"]


def valid_asset(path: Path, row: dict[str, str]) -> bool:
    if not path.is_file() or path.stat().st_size != int(row["size"]):
        return False
    if digest(path) != row["sha256"]:
        return False
    return not row["md5"] or digest(path, "md5") == row["md5"]


def download(row: dict[str, str]) -> Path:
    target = archive_path(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    if valid_asset(target, row):
        return target
    if target.exists():
        fail(f"invalid cached asset; refusing to overwrite: {target}")
    partial = target.with_name(target.name + ".partial")
    subprocess.run([
        "curl", "--fail", "--location", "--show-error", "--retry", "5",
        "--retry-delay", "2", "--retry-all-errors", "--continue-at", "-",
        "--output", str(partial), row["source"],
    ], check=True)
    if not valid_asset(partial, row):
        partial.unlink(missing_ok=True)
        fail(f"checksum or size mismatch for {row['asset_id']}")
    os.replace(partial, target)
    return target


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def install_asset(row: dict[str, str], archive: Path, stage: Path) -> None:
    target = stage / row["install_path"]
    packaging = row["packaging"]
    if packaging == "file":
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["cp", "--reflink=auto", "--", str(archive), str(target)], check=True)
        except subprocess.CalledProcessError:
            shutil.copy2(archive, target)
    elif packaging == "tar.gz":
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as source:
            for member in source.getmembers():
                if not safe_name(member.name) or member.issym() or member.islnk():
                    fail(f"unsafe archive member in {archive.name}: {member.name}")
            source.extractall(target, filter="data")
    elif packaging == "zip":
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                mode = member.external_attr >> 16
                if not safe_name(member.filename) or (mode & 0o170000) == 0o120000:
                    fail(f"unsafe archive member in {archive.name}: {member.filename}")
            source.extractall(target)
    else:
        fail(f"unsupported dataset packaging: {packaging}")


def receipt_rows(rows: list[dict[str, str]]) -> list[str]:
    return [f"{row['asset_id']}={row['sha256']}" for row in rows]


def installed(dataset: str, rows: list[dict[str, str]]) -> bool:
    destination = DATASETS / dataset
    receipt = destination / ".native-asr-dataset"
    if not receipt.is_file():
        return False
    actual = receipt.read_text(encoding="utf-8").splitlines()
    if actual != receipt_rows(rows):
        return False
    expected = max(int(row["expected_count"] or 0) for row in rows)
    if dataset.startswith("librispeech-"):
        return len(list(destination.rglob("*.flac"))) == expected
    if dataset == "ami-es2004a":
        return (destination / "amicorpus/HeadsetAudio/ES2004a.Mix-Headset.wav").is_file()
    return True


def fetch(names: list[str]) -> None:
    DATASETS.mkdir(parents=True, exist_ok=True)
    for dataset, rows in selected(names).items():
        destination = DATASETS / dataset
        if installed(dataset, rows):
            print(f"already valid: {dataset}")
            continue
        if destination.exists():
            fail(f"invalid dataset tree; refusing to overwrite: {destination}")
        archives = [download(row) for row in rows]
        stage = Path(tempfile.mkdtemp(prefix=f".stage.{dataset}.", dir=DATASETS))
        try:
            for row, archive in zip(rows, archives, strict=True):
                install_asset(row, archive, stage)
            (stage / ".native-asr-dataset").write_text(
                "\n".join(receipt_rows(rows)) + "\n", encoding="utf-8"
            )
            os.replace(stage, destination)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        if not installed(dataset, rows):
            fail(f"published dataset failed verification: {dataset}")
        print(f"installed: {dataset} -> {destination}")


def verify(names: list[str]) -> None:
    status = 0
    for dataset, rows in selected(names).items():
        archive_ok = all(valid_asset(archive_path(row), row) for row in rows)
        tree_ok = installed(dataset, rows)
        label = "ok" if archive_ok and tree_ok else "INVALID"
        print(f"{label:<8} {dataset}")
        status |= not (archive_ok and tree_ok)
    raise SystemExit(status)


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def prepare_librispeech(dataset: str, source_root: Path, output_root: Path) -> list[dict]:
    transcript: dict[str, str] = {}
    for text_file in source_root.rglob("*.trans.txt"):
        for line in text_file.read_text(encoding="utf-8").splitlines():
            utterance, text = line.split(" ", 1)
            transcript[utterance] = text
    result = []
    for source in sorted(source_root.rglob("*.flac")):
        utterance = source.stem
        if utterance not in transcript:
            fail(f"missing transcript for {source}")
        prepared = output_root / f"{utterance}.wav"
        prepared.parent.mkdir(parents=True, exist_ok=True)
        if not prepared.exists():
            temporary = prepared.with_name(prepared.name + ".partial")
            subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-map_metadata", "-1", "-vn", "-sn", "-dn",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
            ], check=True)
            os.replace(temporary, prepared)
        speaker = utterance.split("-", 1)[0]
        result.append({
            "utterance_id": utterance, "split": dataset.removeprefix("librispeech-"),
            "source_path": str(source.resolve()), "prepared_path": str(prepared.resolve()),
            "reference": transcript[utterance], "duration_seconds": duration(prepared),
            "speaker": speaker, "source_sha256": digest(source),
        })
    return result


def prepare_ami(source_root: Path, output_root: Path) -> list[dict]:
    source = source_root / "amicorpus/HeadsetAudio/ES2004a.Mix-Headset.wav"
    prepared = output_root / "ES2004a.Mix-Headset.pcm16.wav"
    output_root.mkdir(parents=True, exist_ok=True)
    if not prepared.exists():
        temporary = prepared.with_name(prepared.name + ".partial")
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map_metadata", "-1", "-vn", "-sn", "-dn",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", str(temporary),
        ], check=True)
        os.replace(temporary, prepared)
    return [{
        "utterance_id": "ES2004a", "split": "ami-long-form",
        "source_path": str(source.resolve()), "prepared_path": str(prepared.resolve()),
        "reference": "", "duration_seconds": duration(prepared), "speaker": "overlap-mix",
        "source_sha256": digest(source),
    }]


def prepare(names: list[str]) -> None:
    chosen = selected(names)
    for dataset, rows in chosen.items():
        if not installed(dataset, rows):
            fail(f"dataset is not installed or valid: {dataset}")
        source = DATASETS / dataset
        output = CACHE / "datasets" / "prepared" / dataset
        if dataset.startswith("librispeech-"):
            entries = prepare_librispeech(dataset, source, output)
        elif dataset == "ami-es2004a":
            entries = prepare_ami(source, output)
        else:
            fail(f"no preparer for dataset: {dataset}")
        manifests = DATASETS / "manifests"
        manifests.mkdir(parents=True, exist_ok=True)
        destination = manifests / f"{dataset}.jsonl"
        with tempfile.NamedTemporaryFile("w", dir=manifests, prefix=f".{dataset}.", delete=False) as handle:
            temporary = Path(handle.name)
            for entry in entries:
                handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(temporary, destination)
        print(f"prepared: {dataset} ({len(entries)} utterances) -> {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts/datasets")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    path_parser = sub.add_parser("path")
    path_parser.add_argument("dataset", nargs="?")
    for command in ("fetch", "verify", "prepare"):
        child = sub.add_parser(command)
        child.add_argument("datasets", nargs="*")
    args = parser.parse_args()
    if args.command == "list":
        print(f"{'DATASET':<24} {'LICENSE':<12} NAME")
        for dataset, rows in grouped().items():
            print(f"{dataset:<24} {rows[0]['license']:<12} {rows[0]['name']}")
    elif args.command == "path":
        print(DATASETS / args.dataset if args.dataset else DATASETS)
    elif args.command == "fetch":
        fetch(args.datasets)
    elif args.command == "verify":
        verify(args.datasets)
    elif args.command == "prepare":
        prepare(args.datasets)


if __name__ == "__main__":
    main()
