#!/usr/bin/env python3
"""Validate six held-out ASR runs and atomically publish a reviewed snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from ensemble import DEFAULT_MODELS, _manifest, _rename_noreplace, _sha256, _write_json


SPLITS = ("librispeech-test-clean", "librispeech-test-other")


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(
            encoding="utf-8"
        ).splitlines() if line]
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid JSONL from {path}: {error}") from error


def _ranked_manifest(path: Path, offset: int, limit: int) -> list[dict[str, Any]]:
    rows = _rows(path)
    rows.sort(key=lambda row: (
        hashlib.sha256(row["utterance_id"].encode()).hexdigest(), row["utterance_id"]
    ))
    return rows[offset:offset + limit]


def validate_runs(
    ledger: Path, detail_root: Path, datasets: Path, model_manifest: Path,
    run_ids: list[str], offset: int, limit: int,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if len(run_ids) != 6 or len(set(run_ids)) != 6:
        raise ValueError("exactly six distinct run IDs are required")
    all_runs = _rows(ledger)
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in all_runs:
        by_id.setdefault(row.get("run_id", ""), []).append(row)
    selected = []
    for run_id in run_ids:
        matches = by_id.get(run_id, [])
        if len(matches) != 1:
            raise ValueError(f"run ID must occur exactly once in the ledger: {run_id}")
        selected.append(matches[0])

    expected_cells = {(split, alias) for split in SPLITS for alias in DEFAULT_MODELS}
    actual_cells = {(row.get("dataset"), row.get("model_alias")) for row in selected}
    if actual_cells != expected_cells:
        raise ValueError("run IDs do not cover the required three aliases and two splits")
    revisions = {row.get("git_revision") for row in selected}
    adapters = {row.get("host_adapter_sha256") for row in selected}
    if len(revisions) != 1 or None in revisions or len(adapters) != 1 or None in adapters:
        raise ValueError("all six runs must share one implementation revision and adapter digest")

    records = _manifest(model_manifest)
    details: dict[str, list[dict[str, Any]]] = {}
    expected_by_split = {}
    dataset_digests = {}
    for split in SPLITS:
        manifest_path = datasets / "manifests" / f"{split}.jsonl"
        if not manifest_path.is_file():
            raise ValueError(f"prepared dataset manifest is missing: {manifest_path}")
        expected_by_split[split] = _ranked_manifest(manifest_path, offset, limit)
        if len(expected_by_split[split]) != limit:
            raise ValueError(f"dataset does not contain ranks {offset} through {offset + limit - 1}")
        dataset_digests[split] = _sha256(manifest_path)

    for row in selected:
        run_id = row["run_id"]
        split, alias = row["dataset"], row["model_alias"]
        options = row.get("options", {})
        if (row.get("schema_version") != 2 or row.get("status") != "complete"
                or row.get("failures") != 0 or row.get("utterances") != limit
                or row.get("dirty_tree") is not False):
            raise ValueError(f"run is not a complete clean {limit}-utterance capture: {run_id}")
        if (options.get("subset") != "sha256(utterance_id)"
                or options.get("offset") != offset or options.get("limit") != limit
                or row.get("offset") != offset or row.get("limit") != limit):
            raise ValueError(f"run has the wrong offset/limit selection: {run_id}")
        if (row.get("model_sha256") != records[alias]["sha256"]
                or row.get("dataset_manifest_sha256") != dataset_digests[split]
                or row.get("dataset_digest") != dataset_digests[split]):
            raise ValueError(f"run artifact or dataset digest is stale: {run_id}")
        detail_path = detail_root / f"{run_id}.jsonl"
        if not detail_path.is_file():
            source_path = Path(row.get("detail_path", ""))
            detail_path = source_path if source_path.is_file() else detail_path
        if not detail_path.is_file() or row.get("detail_sha256") != _sha256(detail_path):
            raise ValueError(f"run detail digest is missing or mismatched: {run_id}")
        rows = _rows(detail_path)
        expected = expected_by_split[split]
        if (len(rows) != limit or [item.get("sequence") for item in rows] != list(range(limit))
                or [item.get("utterance_id") for item in rows]
                != [item["utterance_id"] for item in expected]):
            raise ValueError(f"run detail ordering or completeness is invalid: {run_id}")
        for item, manifest_item in zip(rows, expected):
            if (item.get("run_id") != run_id or item.get("split") != split
                    or item.get("exit_status") != 0 or item.get("stderr") is not None
                    or item.get("source_sha256") != manifest_item["source_sha256"]
                    or item.get("reference_raw") != manifest_item["reference"]):
                raise ValueError(f"run detail identity is invalid: {run_id}")
        details[run_id] = rows

    for split in SPLITS:
        orderings = [
            [item["utterance_id"] for item in details[row["run_id"]]]
            for row in selected if row["dataset"] == split
        ]
        if not all(ordering == orderings[0] for ordering in orderings[1:]):
            raise ValueError(f"utterance ordering differs across aliases: {split}")
        for sequence in range(limit):
            identities = {
                (details[row["run_id"]][sequence]["source_sha256"],
                 details[row["run_id"]][sequence]["reference_raw"])
                for row in selected if row["dataset"] == split
            }
            if len(identities) != 1:
                raise ValueError(f"source identity differs across aliases: {split} #{sequence}")

    selected.sort(key=lambda row: (
        SPLITS.index(row["dataset"]), DEFAULT_MODELS.index(row["model_alias"])
    ))
    metadata = {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_ledger_sha256": _sha256(ledger),
        "selection": {
            "ranking": "sha256(utterance_id)", "offset": offset, "limit": limit,
        },
        "git_revision": next(iter(revisions)),
        "host_adapter_sha256": next(iter(adapters)),
        "splits": list(SPLITS),
        "model_aliases": list(DEFAULT_MODELS),
        "run_ids": [row["run_id"] for row in selected],
        "details": {run_id: {"sha256": row["detail_sha256"]}
                    for run_id, row in ((item["run_id"], item) for item in selected)},
    }
    return selected, details, metadata


def publish_snapshot(
    output: Path, runs: list[dict[str, Any]], details: dict[str, list[dict[str, Any]]],
    metadata: dict[str, Any],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(output):
        raise ValueError(f"output already exists: {output}")
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage.", dir=output.parent))
    try:
        os.chmod(stage, 0o755)
        detail_dir = stage / "details"
        detail_dir.mkdir(mode=0o755)
        runs_payload = "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in runs
        )
        (stage / "runs.jsonl").write_text(runs_payload, encoding="utf-8")
        os.chmod(stage / "runs.jsonl", 0o644)
        for run in runs:
            run_id = run["run_id"]
            payload = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in details[run_id]
            )
            path = detail_dir / f"{run_id}.jsonl"
            path.write_text(payload, encoding="utf-8")
            os.chmod(path, 0o644)
            if _sha256(path) != run["detail_sha256"]:
                raise ValueError(f"published detail digest changed: {run_id}")
        _write_json(stage / "snapshot.json", metadata)
        os.chmod(stage / "snapshot.json", 0o644)
        readme = (
            "# LibriSpeech held-out 100-rank snapshot\n\n"
            f"This reviewed snapshot contains SHA-256 ranks {metadata['selection']['offset']}–"
            f"{metadata['selection']['offset'] + metadata['selection']['limit'] - 1} for both "
            "LibriSpeech test splits and the three ensemble aliases. `snapshot.json` records "
            "the validated run and detail digests.\n"
        )
        (stage / "README.md").write_text(readme, encoding="utf-8")
        os.chmod(stage / "README.md", 0o644)
        _rename_noreplace(stage, output)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--ledger", type=Path, default=Path(os.environ["NATIVE_ASR_BENCHMARKS"]))
    result.add_argument("--details", type=Path)
    result.add_argument("--datasets", type=Path, default=Path(os.environ["NATIVE_ASR_DATASETS"]))
    result.add_argument("--model-manifest", type=Path, default=Path(os.environ.get(
        "NATIVE_ASR_MODEL_MANIFEST", root / "manifests/models.lock"
    )))
    result.add_argument("--offset", type=int, required=True)
    result.add_argument("--limit", type=int, required=True)
    result.add_argument("run_id", nargs=6)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.offset < 0 or args.limit <= 0:
        raise SystemExit("error: --offset must be non-negative and --limit must be positive")
    ledger = args.ledger.expanduser().resolve(strict=True)
    detail_root = (args.details.expanduser().resolve(strict=True) if args.details
                   else ledger.parent / "details")
    try:
        runs, details, metadata = validate_runs(
            ledger, detail_root, args.datasets.expanduser().resolve(strict=True),
            args.model_manifest.expanduser().resolve(strict=True), args.run_id,
            args.offset, args.limit,
        )
        publish_snapshot(args.output.expanduser().absolute(), runs, details, metadata)
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error
    print(f"reviewed snapshot: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
