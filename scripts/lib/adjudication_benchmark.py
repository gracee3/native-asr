#!/usr/bin/env python3
"""Repeatable real-model bake-off over the committed 200-utterance snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from ensemble import (
    ADJUDICATION_PROTOCOL_VERSION,
    DEFAULT_MODELS,
    EnsembleJob,
    LLAMA_CPP_REVISION,
    _json_bytes,
    _manifest,
    _sha256,
    _write_json,
    adjudication_prompt,
    adjudicator_configuration,
    build_consensus,
    lexical_tokens,
    render_adjudicated,
    validate_adjudication_choices,
)
from evaluation import errors


DEFAULT_ADJUDICATORS = (
    "llm:ministral-3b-instruct-2512",
    "llm:ministral-8b-instruct-2512",
)
SPLITS = ("librispeech-test-clean", "librispeech-test-other")
EXPECTED = {
    "spans": 141,
    "columns": 244,
    "baseline_errors": {
        "librispeech-test-clean": 37,
        "librispeech-test-other": 54,
    },
    "oracle_errors": {
        "librispeech-test-clean": 27,
        "librispeech-test-other": 40,
    },
}


def load_snapshot(snapshot: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    aliases = list(DEFAULT_MODELS)
    runs = [json.loads(line) for line in (snapshot / "runs.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()]
    corpora: dict[str, list[dict[str, Any]]] = {}
    for split in SPLITS:
        tables = []
        for alias in aliases:
            run = next(row for row in runs
                       if row["dataset"] == split and row["model_alias"] == alias)
            rows = [json.loads(line) for line in (
                snapshot / "details" / f"{run['run_id']}.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            tables.append({row["utterance_id"]: row for row in rows})
        if not (list(tables[0]) == list(tables[1]) == list(tables[2])):
            raise RuntimeError(f"snapshot utterance order differs across tracks: {split}")
        corpus = []
        for utterance_id in tables[0]:
            source_rows = [table[utterance_id] for table in tables]
            tracks = [{
                "model_alias": alias,
                "normalized_tokens": lexical_tokens(row["hypothesis_raw"]),
            } for alias, row in zip(aliases, source_rows)]
            corpus.append({
                "utterance_id": utterance_id,
                "reference": source_rows[0]["reference_raw"],
                "tracks": tracks,
                "consensus": build_consensus(tracks),
            })
        corpora[split] = corpus
    return corpora, aliases


def benchmark_request(
    job: EnsembleJob, process: subprocess.Popen[str], prompt: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    span_index = prompt["input"]["span_index"]
    request_id = f"span-{span_index}"
    request = {
        "command": "adjudicate",
        "request_id": request_id,
        "prompt": prompt,
        "max_tokens": min(1024, max(128, 40 * len(prompt["input"]["columns"]) + 64)),
    }
    assert process.stdin is not None
    started = time.monotonic()
    process.stdin.write(_json_bytes(request).decode("utf-8"))
    process.stdin.flush()
    line = job._read_worker_line(process, job.adjudication_timeout)
    wall = time.monotonic() - started
    envelope = json.loads(line)
    if not isinstance(envelope, dict) or envelope.get("request_id") != request_id:
        raise RuntimeError("worker response identity does not match the request")
    if envelope.get("error"):
        raise RuntimeError(f"worker error: {envelope['error']}")
    response = envelope.get("response")
    if not isinstance(response, dict):
        raise RuntimeError("worker response has no server object")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise RuntimeError("server response must contain exactly one choice")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("server response has no JSON content")
    validated = validate_adjudication_choices(prompt, json.loads(content))
    timing = {"wall_seconds": wall, **job._response_timing(response)}
    return validated, timing


def one_repeat(
    root: Path, records: dict[str, dict[str, str]], env: dict[str, str],
    alias: str, timeout: float, corpora: dict[str, list[dict[str, Any]]], repeat: int,
) -> dict[str, Any]:
    stage = Path(tempfile.mkdtemp(prefix="native-asr-adjudication-benchmark."))
    os.chmod(stage, 0o700)
    (stage / "logs").mkdir(mode=0o700)
    (stage / "logs/adjudicator.stderr.log").touch(mode=0o600)
    job = EnsembleJob(
        root, stage / "unused-output", Path("/dev/null"), list(DEFAULT_MODELS),
        records, env, adjudicator_alias=alias, adjudication_timeout=timeout,
    )
    job.stage = stage
    job.adjudicator = adjudicator_configuration(
        root, alias, records, env, job.engine
    )
    if not job.adjudicator["available"]:
        raise RuntimeError(job.adjudicator["unavailable_reason"])
    process: subprocess.Popen[str] | None = None
    worker_metrics = None
    span_records: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    scores: dict[str, dict[str, int]] = {}
    selected_by_utterance: dict[tuple[str, str], dict[int, int]] = {}
    load_seconds = None
    worker_started = None
    try:
        process, load_seconds, worker_started = job._start_worker()
        requests = []
        for split_order, split in enumerate(SPLITS):
            for utterance_order, utterance in enumerate(corpora[split]):
                consensus = utterance["consensus"]
                key = (split, utterance["utterance_id"])
                selected_by_utterance[key] = {}
                for span in consensus["disagreements"]:
                    prompt = adjudication_prompt(consensus, span)
                    requests.append({
                        "sort_key": (
                            len(prompt["input"]["columns"]), split_order,
                            utterance_order, span["index"],
                        ),
                        "split": split,
                        "utterance_id": utterance["utterance_id"],
                        "span_index": span["index"],
                        "prompt": prompt,
                    })
        # Requests with the largest response schemas run last. If a bounded request
        # times out and the JSONL stream can no longer be resynchronized, this keeps
        # the failure local to the few genuinely oversized spans.
        requests.sort(key=lambda item: item["sort_key"])
        worker_lost_reason = None
        for request in requests:
            timing = None
            validated = None
            fallback_reason = worker_lost_reason
            started = time.monotonic()
            if worker_lost_reason is None:
                try:
                    assert process is not None
                    validated, timing = benchmark_request(
                        job, process, request["prompt"]
                    )
                except TimeoutError as error:
                    timing = {"wall_seconds": time.monotonic() - started}
                    fallback_reason = str(error)
                    drain_started = time.monotonic()
                    try:
                        assert process is not None
                        request_id = f"span-{request['prompt']['input']['span_index']}"
                        job._drain_late_response(process, request_id)
                        timing["drain_wall_seconds"] = time.monotonic() - drain_started
                        timing["late_response_discarded"] = True
                    except (TimeoutError, BrokenPipeError, OSError,
                            json.JSONDecodeError, ValueError) as drain_error:
                        worker_lost_reason = f"late response drain failed: {drain_error}"
                        worker_metrics = job._stop_worker(process, force=True)
                        process = None
                except (BrokenPipeError, OSError) as error:
                    timing = {"wall_seconds": time.monotonic() - started}
                    fallback_reason = f"worker failure: {error}"
                    worker_lost_reason = fallback_reason
                    worker_metrics = job._stop_worker(process, force=True)
                    process = None
                except (json.JSONDecodeError, ValueError, TypeError, KeyError) as error:
                    timing = {"wall_seconds": time.monotonic() - started}
                    fallback_reason = f"invalid response: {error}"
            if validated is not None:
                selected = selected_by_utterance[
                    (request["split"], request["utterance_id"])
                ]
                for choice in validated:
                    selected[choice["column_index"]] = choice["candidate_index"]
            record = {
                "split": request["split"],
                "utterance_id": request["utterance_id"],
                "span_index": request["span_index"],
                "choices": validated,
                "timing": timing,
                "fallback_reason": fallback_reason,
            }
            decisions.append(record)
            span_records.append(record)

        for split in SPLITS:
            total_errors = total_words = 0
            for utterance in corpora[split]:
                selected = selected_by_utterance[(split, utterance["utterance_id"])]
                hypothesis = render_adjudicated(
                    utterance["consensus"], utterance["tracks"], selected
                )
                counts = errors(utterance["reference"], hypothesis)
                total_errors += counts["errors"]
                total_words += counts["reference_words"]
            scores[split] = {"errors": total_errors, "reference_words": total_words}
    finally:
        if process is not None:
            worker_metrics = job._stop_worker(process, force=False)
        stderr = (stage / "logs/adjudicator.stderr.log").read_text(
            encoding="utf-8", errors="replace"
        )
        shutil.rmtree(stage)
    execution = job._execution_summary(
        load_seconds,
        None if worker_started is None else time.monotonic() - worker_started,
        worker_metrics,
        span_records,
    )
    canonical = [{key: decision[key] for key in (
        "split", "utterance_id", "span_index", "choices", "fallback_reason",
    )} for decision in decisions]
    digest = hashlib.sha256(_json_bytes(canonical)).hexdigest()
    return {
        "repeat": repeat,
        "provenance": {
            "artifact": job.adjudicator["artifact"],
            "container": job.adjudicator["container"],
            "runtime": job.adjudicator["runtime_provenance"],
            "policy": job.adjudicator["policy"],
        },
        "scores": scores,
        "combined_errors": sum(item["errors"] for item in scores.values()),
        "decisions_sha256": digest,
        "decisions": decisions,
        "validated_spans": sum(item["choices"] is not None for item in decisions),
        "fallback_spans": sum(item["choices"] is None for item in decisions),
        "execution": execution,
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }


def summarize_model(alias: str, repeats: list[dict[str, Any]]) -> dict[str, Any]:
    decision_digests = {item["decisions_sha256"] for item in repeats}
    score_shapes = {json.dumps(item["scores"], sort_keys=True) for item in repeats}
    latencies = [
        decision["timing"]["wall_seconds"]
        for repeat in repeats for decision in repeat["decisions"]
        if isinstance(decision.get("timing"), dict)
        and isinstance(decision["timing"].get("wall_seconds"), (int, float))
    ]
    prompt_tokens = sum(repeat["execution"]["prompt_tokens"] for repeat in repeats)
    generated_tokens = sum(repeat["execution"]["generated_tokens"] for repeat in repeats)
    prompt_seconds = sum(repeat["execution"]["prompt_seconds"] for repeat in repeats)
    generation_seconds = sum(repeat["execution"]["generation_seconds"] for repeat in repeats)
    peak_rss_values = [
        repeat["execution"]["peak_rss_kb"] for repeat in repeats
        if repeat["execution"]["peak_rss_kb"] is not None
    ]
    ordered = sorted(latencies)
    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    first = repeats[0]
    qualifies = (
        len(decision_digests) == 1
        and len(score_shapes) == 1
        and first["scores"][SPLITS[0]]["errors"] <= EXPECTED["baseline_errors"][SPLITS[0]]
        and first["scores"][SPLITS[1]]["errors"] <= EXPECTED["baseline_errors"][SPLITS[1]]
        and first["combined_errors"] < sum(EXPECTED["baseline_errors"].values())
    )
    return {
        "alias": alias,
        "provenance": repeats[0]["provenance"],
        "identical_validated_decisions": len(decision_digests) == 1,
        "identical_scores": len(score_shapes) == 1,
        "validated_spans": [repeat["validated_spans"] for repeat in repeats],
        "fallback_spans": [repeat["fallback_spans"] for repeat in repeats],
        "qualifies": qualifies,
        "scores": first["scores"],
        "combined_errors": first["combined_errors"],
        "metrics": {
            "load_seconds": [repeat["execution"]["load_seconds"] for repeat in repeats],
            "prompt_tokens_per_second": prompt_tokens / prompt_seconds,
            "generation_tokens_per_second": generated_tokens / generation_seconds,
            "span_latency_seconds": {"p50": percentile(0.50), "p95": percentile(0.95)},
            "cpu_seconds": [repeat["execution"]["cpu_seconds"] for repeat in repeats],
            "peak_rss_kb": max(peak_rss_values, default=None),
        },
        "repeats": repeats,
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--snapshot", type=Path, default=(
        root / "benchmarks/published/2026-08-09-librispeech-100"
    ))
    result.add_argument("--adjudicator", action="append", default=[])
    result.add_argument(
        "--reuse-model-result", action="append", default=[], type=Path,
        help="reuse a previously completed model result after strict provenance checks",
    )
    result.add_argument("--repeats", type=int, default=2)
    result.add_argument("--timeout", type=float, default=30.0)
    return result


def main() -> int:
    os.umask(0o077)
    args = parser().parse_args()
    if args.repeats < 2 or args.timeout <= 0:
        raise SystemExit("error: --repeats must be at least two and --timeout must be positive")
    output = args.output.expanduser().absolute()
    if os.path.lexists(output):
        raise SystemExit(f"error: output already exists: {output}")
    root = Path(__file__).resolve().parents[2]
    snapshot = args.snapshot.expanduser().resolve(strict=True)
    env = dict(os.environ)
    records = _manifest(Path(env.get(
        "NATIVE_ASR_MODEL_MANIFEST", root / "manifests/models.lock"
    )))
    aliases = args.adjudicator or list(DEFAULT_ADJUDICATORS)
    for alias in aliases:
        if alias not in records or records[alias]["runtime"] != "llama-cpp":
            raise SystemExit(f"error: invalid adjudicator alias: {alias}")
    subprocess.run([str(root / "scripts/verify-models"), *aliases], env=env, check=True)
    corpora, asr_aliases = load_snapshot(snapshot)
    spans = sum(len(item["consensus"]["disagreements"])
                for corpus in corpora.values() for item in corpus)
    columns = sum(item["consensus"]["decision_counts"]["non_unanimous"]
                  for corpus in corpora.values() for item in corpus)
    baseline = {}
    for split, corpus in corpora.items():
        count = sum(errors(item["reference"], item["consensus"]["text"])["errors"]
                    for item in corpus)
        baseline[split] = count
    if spans != EXPECTED["spans"] or columns != EXPECTED["columns"]:
        raise SystemExit(f"error: snapshot shape changed: {spans} spans, {columns} columns")
    if baseline != EXPECTED["baseline_errors"]:
        raise SystemExit(f"error: deterministic baseline changed: {baseline}")

    reused_models: dict[str, dict[str, Any]] = {}
    reuse_sources = []
    for reuse_path_arg in args.reuse_model_result:
        reuse_path = reuse_path_arg.expanduser().resolve(strict=True)
        reused = json.loads(reuse_path.read_text(encoding="utf-8"))
        reused_snapshot = reused.get("snapshot") if isinstance(reused, dict) else None
        if (not isinstance(reused_snapshot, dict)
                or reused_snapshot.get("runs_sha256") != _sha256(snapshot / "runs.jsonl")
                or reused_snapshot.get("spans") != spans
                or reused_snapshot.get("non_unanimous_columns") != columns):
            raise SystemExit(f"error: reused result has different snapshot provenance: {reuse_path}")
        source_aliases = []
        for model in reused.get("models", []):
            alias = model.get("alias") if isinstance(model, dict) else None
            if alias not in aliases:
                continue
            if alias in reused_models:
                raise SystemExit(f"error: duplicate reused model result: {alias}")
            repeats = model.get("repeats", [])
            if len(repeats) != args.repeats:
                raise SystemExit(f"error: reused model has the wrong repeat count: {alias}")
            if any(
                repeat.get("provenance", {}).get("artifact", {}).get("sha256")
                != records[alias]["sha256"]
                or repeat.get("provenance", {}).get("runtime", {}).get("revision")
                != LLAMA_CPP_REVISION
                for repeat in repeats
            ):
                raise SystemExit(f"error: reused model provenance is stale: {alias}")
            reused_models[alias] = summarize_model(alias, repeats)
            source_aliases.append(alias)
        reuse_sources.append({
            "sha256": _sha256(reuse_path),
            "aliases": source_aliases,
        })

    models = []
    for alias in aliases:
        if alias in reused_models:
            print(f"adjudication benchmark: reusing validated result for {alias}", flush=True)
            models.append(reused_models[alias])
            continue
        repeats = []
        for repeat in range(1, args.repeats + 1):
            print(f"adjudication benchmark: {alias} repeat {repeat}", flush=True)
            repeats.append(one_repeat(
                root, records, env, alias, args.timeout, corpora, repeat
            ))
        models.append(summarize_model(alias, repeats))
    qualified = sorted(
        (model for model in models if model["qualifies"]),
        key=lambda model: (
            model["combined_errors"],
            (model["metrics"]["span_latency_seconds"]["p95"]
             if model["metrics"]["span_latency_seconds"]["p95"] is not None
             else float("inf")),
            (model["metrics"]["peak_rss_kb"]
             if model["metrics"]["peak_rss_kb"] is not None else float("inf")),
        ),
    )
    recommendation = {
        "status": "qualified" if qualified else "blocked",
        "alias": qualified[0]["alias"] if qualified else None,
        "ranking_policy": "combined_errors_then_p95_latency_then_peak_rss",
    }
    result = {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_revision": subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip(),
        "git_dirty": bool(subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"], text=True,
            capture_output=True, check=True,
        ).stdout),
        "snapshot": {
            "path": str(snapshot.relative_to(root)),
            "runs_sha256": _sha256(snapshot / "runs.jsonl"),
            "utterances": sum(len(corpus) for corpus in corpora.values()),
            "spans": spans,
            "non_unanimous_columns": columns,
            "asr_aliases": asr_aliases,
        },
        "gates": {
            "deterministic_baseline_errors": EXPECTED["baseline_errors"],
            "selection_only_oracle_errors": EXPECTED["oracle_errors"],
            "maximum_split_errors": EXPECTED["baseline_errors"],
            "combined_must_be_less_than": sum(EXPECTED["baseline_errors"].values()),
            "identical_validated_decisions_required": True,
        },
        "models": models,
        "reused_model_results": reuse_sources,
        "recommendation": recommendation,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, result)
    print(f"adjudication benchmark: {recommendation['status']}: {recommendation['alias']}")
    return 0 if qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
