#!/usr/bin/env python3
"""Repeatable tie-only local-LLM evaluation on calibration and validation snapshots."""

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
    ADJUDICATION_POLICY_ID,
    ADJUDICATION_PROTOCOL_VERSION,
    BoundaryPathConflict,
    DEFAULT_MODELS,
    EnsembleJob,
    LLAMA_CPP_REVISION,
    _json_bytes,
    _manifest,
    _sha256,
    _write_json,
    adjudication_prompt,
    adjudication_spans,
    adjudicator_configuration,
    build_consensus,
    lexical_tokens,
    render_adjudicated,
    validate_adjudication_choices,
    validate_boundary_paths,
)
from evaluation import errors


DEFAULT_ADJUDICATORS = (
    "llm:ministral-3b-instruct-2512",
    "llm:ministral-8b-instruct-2512",
)
SPLITS = ("librispeech-test-clean", "librispeech-test-other")
CALIBRATION_BASELINE = {
    "librispeech-test-clean": 37,
    "librispeech-test-other": 54,
}


def load_snapshot(snapshot: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    aliases = list(DEFAULT_MODELS)
    runs = [json.loads(line) for line in (snapshot / "runs.jsonl").read_text(
        encoding="utf-8"
    ).splitlines() if line]
    corpora: dict[str, list[dict[str, Any]]] = {}
    for split in SPLITS:
        tables = []
        for alias in aliases:
            matches = [row for row in runs
                       if row.get("dataset") == split and row.get("model_alias") == alias]
            if len(matches) != 1 or matches[0].get("status") != "complete":
                raise RuntimeError(f"snapshot must contain one complete {alias} / {split} run")
            run = matches[0]
            detail = snapshot / "details" / f"{run['run_id']}.jsonl"
            rows = [json.loads(line) for line in detail.read_text(
                encoding="utf-8"
            ).splitlines() if line]
            if len(rows) != run.get("utterances") or any(
                row.get("run_id") != run["run_id"] or row.get("exit_status") != 0
                for row in rows
            ):
                raise RuntimeError(f"snapshot detail is incomplete: {run['run_id']}")
            tables.append({row["utterance_id"]: row for row in rows})
        if not (list(tables[0]) == list(tables[1]) == list(tables[2])):
            raise RuntimeError(f"snapshot utterance order differs across tracks: {split}")
        corpus = []
        for utterance_id in tables[0]:
            source_rows = [table[utterance_id] for table in tables]
            if len({(row["reference_raw"], row["source_sha256"]) for row in source_rows}) != 1:
                raise RuntimeError(f"snapshot source identity differs: {split} / {utterance_id}")
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


def evaluation_shape(corpora: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    baseline = {}
    tie_spans = tie_columns = protected_majority = non_unanimous = 0
    for split, corpus in corpora.items():
        baseline[split] = sum(
            errors(item["reference"], item["consensus"]["text"])["errors"]
            for item in corpus
        )
        for item in corpus:
            consensus = item["consensus"]
            spans = adjudication_spans(consensus)
            tie_spans += len(spans)
            tie_columns += sum(
                span["end_column_exclusive"] - span["start_column"] for span in spans
            )
            protected_majority += (
                consensus["decision_counts"]["majority_token"]
                + consensus["decision_counts"]["majority_deletion"]
            )
            non_unanimous += consensus["decision_counts"]["non_unanimous"]
    return {
        "utterances": sum(len(corpus) for corpus in corpora.values()),
        "eligible_tie_spans": tie_spans,
        "eligible_tie_columns": tie_columns,
        "protected_majority_columns": protected_majority,
        "non_unanimous_columns": non_unanimous,
        "deterministic_baseline_errors": baseline,
    }


def benchmark_request(
    job: EnsembleJob, process: subprocess.Popen[str], prompt: dict[str, Any], request_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    job.adjudicator = adjudicator_configuration(root, alias, records, env, job.engine)
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
                for span in adjudication_spans(consensus):
                    prompt = adjudication_prompt(consensus, span)
                    requests.append({
                        "sort_key": (
                            len(prompt["input"]["columns"]), split_order,
                            utterance_order, span["index"],
                        ),
                        "split": split,
                        "utterance_id": utterance["utterance_id"],
                        "span": span,
                        "consensus": consensus,
                        "prompt": prompt,
                    })
        requests.sort(key=lambda item: item["sort_key"])
        worker_lost_reason = None
        for sequence, request in enumerate(requests):
            timing = None
            validated = None
            fallback_reason = worker_lost_reason
            fallback_code = "worker_unavailable" if worker_lost_reason else None
            request_id = f"request-{sequence}"
            started = time.monotonic()
            if worker_lost_reason is None:
                try:
                    assert process is not None
                    validated, timing = benchmark_request(
                        job, process, request["prompt"], request_id
                    )
                    validate_boundary_paths(
                        request["consensus"], request["span"], validated
                    )
                except TimeoutError as error:
                    timing = {"wall_seconds": time.monotonic() - started}
                    fallback_reason, fallback_code = str(error), "timeout"
                    drain_started = time.monotonic()
                    try:
                        assert process is not None
                        job._drain_late_response(process, request_id)
                        timing["drain_wall_seconds"] = time.monotonic() - drain_started
                        timing["late_response_discarded"] = True
                    except (TimeoutError, BrokenPipeError, OSError,
                            json.JSONDecodeError, ValueError) as drain_error:
                        worker_lost_reason = f"late response drain failed: {drain_error}"
                        worker_metrics = job._stop_worker(process, force=True)
                        process = None
                except BoundaryPathConflict as error:
                    timing = timing or {"wall_seconds": time.monotonic() - started}
                    fallback_reason, fallback_code = str(error), "boundary_path_conflict"
                    validated = None
                except (BrokenPipeError, OSError, RuntimeError) as error:
                    timing = {"wall_seconds": time.monotonic() - started}
                    fallback_reason = f"worker failure: {error}"
                    fallback_code = "worker_failure"
                    worker_lost_reason = fallback_reason
                    worker_metrics = job._stop_worker(process, force=True)
                    process = None
                except (json.JSONDecodeError, ValueError, TypeError, KeyError) as error:
                    timing = {"wall_seconds": time.monotonic() - started}
                    fallback_reason = f"invalid response: {error}"
                    fallback_code = "invalid_response"
                    validated = None
            if validated is not None:
                selected = selected_by_utterance[
                    (request["split"], request["utterance_id"])
                ]
                for choice in validated:
                    selected[choice["column_index"]] = choice["candidate_index"]
            record = {
                "split": request["split"],
                "utterance_id": request["utterance_id"],
                "span_index": request["span"]["index"],
                "choices": validated,
                "timing": timing,
                "fallback_reason": fallback_reason,
                "fallback_code": fallback_code,
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
        "split", "utterance_id", "span_index", "choices", "fallback_code",
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


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_evaluation(
    repeats: list[dict[str, Any]], baseline: dict[str, int],
) -> dict[str, Any]:
    decision_digests = {item["decisions_sha256"] for item in repeats}
    score_shapes = {json.dumps(item["scores"], sort_keys=True) for item in repeats}
    latencies = [
        decision["timing"]["wall_seconds"]
        for repeat in repeats for decision in repeat["decisions"]
        if isinstance(decision.get("timing"), dict)
        and isinstance(decision["timing"].get("wall_seconds"), (int, float))
    ]
    executions = [repeat["execution"] or {} for repeat in repeats]
    prompt_tokens = sum(item.get("prompt_tokens", 0) for item in executions)
    generated_tokens = sum(item.get("generated_tokens", 0) for item in executions)
    prompt_seconds = sum(item.get("prompt_seconds", 0) for item in executions)
    generation_seconds = sum(item.get("generation_seconds", 0) for item in executions)
    peak_rss_values = [item["peak_rss_kb"] for item in executions
                       if item.get("peak_rss_kb") is not None]
    first = repeats[0]
    identical_decisions = len(decision_digests) == 1
    identical_scores = len(score_shapes) == 1
    zero_fallbacks = all(repeat["fallback_spans"] == 0 for repeat in repeats)
    split_not_worse = all(first["scores"][split]["errors"] <= baseline[split]
                          for split in SPLITS)
    strict_combined_improvement = first["combined_errors"] < sum(baseline.values())
    qualifies = (identical_decisions and identical_scores and zero_fallbacks
                 and split_not_worse and strict_combined_improvement)
    return {
        "identical_validated_decisions": identical_decisions,
        "identical_scores": identical_scores,
        "zero_fallback_spans": zero_fallbacks,
        "split_not_worse_than_baseline": split_not_worse,
        "strict_combined_improvement": strict_combined_improvement,
        "qualifies": qualifies,
        "validated_spans": [repeat["validated_spans"] for repeat in repeats],
        "fallback_spans": [repeat["fallback_spans"] for repeat in repeats],
        "scores": first["scores"],
        "combined_errors": first["combined_errors"],
        "metrics": {
            "load_seconds": [item.get("load_seconds") for item in executions],
            "prompt_tokens_per_second": (
                prompt_tokens / prompt_seconds if prompt_seconds > 0 else None
            ),
            "generation_tokens_per_second": (
                generated_tokens / generation_seconds if generation_seconds > 0 else None
            ),
            "span_latency_seconds": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
            "cpu_seconds": [item.get("cpu_seconds") for item in executions],
            "peak_rss_kb": max(peak_rss_values, default=None),
        },
        "repeats": repeats,
    }


def summarize_model(
    alias: str, repeats_by_evaluation: dict[str, list[dict[str, Any]]],
    baselines: dict[str, dict[str, int]],
) -> dict[str, Any]:
    evaluations = {
        name: summarize_evaluation(repeats, baselines[name])
        for name, repeats in repeats_by_evaluation.items()
    }
    return {
        "alias": alias,
        "provenance": repeats_by_evaluation["calibration"][0]["provenance"],
        "policy_id": ADJUDICATION_POLICY_ID,
        "qualifies": all(item["qualifies"] for item in evaluations.values()),
        "evaluations": evaluations,
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--calibration-snapshot", type=Path, default=(
        root / "benchmarks/published/2026-08-09-librispeech-100"
    ))
    result.add_argument("--validation-snapshot", type=Path, default=(
        root / "benchmarks/published/2026-08-10-librispeech-heldout-100"
    ))
    result.add_argument("--adjudicator", action="append", default=[])
    result.add_argument(
        "--reuse-model-result", action="append", default=[], type=Path,
        help="reuse schema-2 model repeats after strict snapshot and runtime checks",
    )
    result.add_argument("--repeats", type=int, default=2)
    result.add_argument("--timeout", type=float, default=30.0)
    return result


def _snapshot_metadata(
    root: Path, path: Path, shape: dict[str, Any], aliases: list[str],
) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "runs_sha256": _sha256(path / "runs.jsonl"),
        **shape,
        "asr_aliases": aliases,
    }


def main() -> int:
    os.umask(0o077)
    args = parser().parse_args()
    if args.repeats < 2 or args.timeout <= 0:
        raise SystemExit("error: --repeats must be at least two and --timeout must be positive")
    output = args.output.expanduser().absolute()
    if os.path.lexists(output):
        raise SystemExit(f"error: output already exists: {output}")
    root = Path(__file__).resolve().parents[2]
    snapshot_paths = {
        "calibration": args.calibration_snapshot.expanduser().resolve(strict=True),
        "validation": args.validation_snapshot.expanduser().resolve(strict=True),
    }
    env = dict(os.environ)
    records = _manifest(Path(env.get(
        "NATIVE_ASR_MODEL_MANIFEST", root / "manifests/models.lock"
    )))
    aliases = args.adjudicator or list(DEFAULT_ADJUDICATORS)
    for alias in aliases:
        if alias not in records or records[alias]["runtime"] != "llama-cpp":
            raise SystemExit(f"error: invalid adjudicator alias: {alias}")
    subprocess.run([str(root / "scripts/verify-models"), *aliases], env=env, check=True)

    corpora = {}
    shapes = {}
    snapshot_metadata = {}
    asr_aliases = None
    for name, path in snapshot_paths.items():
        corpora[name], loaded_aliases = load_snapshot(path)
        if asr_aliases is not None and loaded_aliases != asr_aliases:
            raise SystemExit("error: ASR aliases differ between evaluation snapshots")
        asr_aliases = loaded_aliases
        shapes[name] = evaluation_shape(corpora[name])
        snapshot_metadata[name] = _snapshot_metadata(
            root, path, shapes[name], loaded_aliases
        )
    if shapes["calibration"]["deterministic_baseline_errors"] != CALIBRATION_BASELINE:
        raise SystemExit(
            "error: disabled-adjudication calibration regression changed: "
            f"{shapes['calibration']['deterministic_baseline_errors']}"
        )
    baselines = {
        name: shape["deterministic_baseline_errors"] for name, shape in shapes.items()
    }

    reused_models: dict[str, dict[str, Any]] = {}
    reuse_sources = []
    for reuse_path_arg in args.reuse_model_result:
        reuse_path = reuse_path_arg.expanduser().resolve(strict=True)
        reused = json.loads(reuse_path.read_text(encoding="utf-8"))
        if reused.get("schema_version") != 2 or any(
            reused.get("evaluations", {}).get(name, {}).get("snapshot", {}).get("runs_sha256")
            != snapshot_metadata[name]["runs_sha256"] for name in snapshot_paths
        ):
            raise SystemExit(f"error: reused result has different snapshot provenance: {reuse_path}")
        source_aliases = []
        for model in reused.get("models", []):
            alias = model.get("alias") if isinstance(model, dict) else None
            if alias not in aliases:
                continue
            if alias in reused_models:
                raise SystemExit(f"error: duplicate reused model result: {alias}")
            repeats_by_evaluation = {
                name: model.get("evaluations", {}).get(name, {}).get("repeats", [])
                for name in snapshot_paths
            }
            if any(len(repeats) != args.repeats
                   for repeats in repeats_by_evaluation.values()):
                raise SystemExit(f"error: reused model has the wrong repeat count: {alias}")
            if any(
                repeat.get("provenance", {}).get("artifact", {}).get("sha256")
                != records[alias]["sha256"]
                or repeat.get("provenance", {}).get("runtime", {}).get("revision")
                != LLAMA_CPP_REVISION
                or repeat.get("provenance", {}).get("policy", {}).get(
                    "adjudication_policy_id"
                ) != ADJUDICATION_POLICY_ID
                for repeats in repeats_by_evaluation.values() for repeat in repeats
            ):
                raise SystemExit(f"error: reused model provenance is stale: {alias}")
            reused_models[alias] = summarize_model(alias, repeats_by_evaluation, baselines)
            source_aliases.append(alias)
        reuse_sources.append({"sha256": _sha256(reuse_path), "aliases": source_aliases})

    models = []
    for alias in aliases:
        if alias in reused_models:
            print(f"adjudication benchmark: reusing validated result for {alias}", flush=True)
            models.append(reused_models[alias])
            continue
        repeats_by_evaluation = {}
        for name in ("calibration", "validation"):
            repeats = []
            for repeat in range(1, args.repeats + 1):
                print(f"adjudication benchmark: {alias} {name} repeat {repeat}", flush=True)
                repeats.append(one_repeat(
                    root, records, env, alias, args.timeout, corpora[name], repeat
                ))
            repeats_by_evaluation[name] = repeats
        models.append(summarize_model(alias, repeats_by_evaluation, baselines))

    qualified = sorted(
        (model for model in models if model["qualifies"]),
        key=lambda model: (
            model["evaluations"]["validation"]["combined_errors"],
            (model["evaluations"]["validation"]["metrics"]
             ["span_latency_seconds"]["p95"] or float("inf")),
            (model["evaluations"]["validation"]["metrics"]["peak_rss_kb"]
             or float("inf")),
        ),
    )
    recommendation = {
        "status": "qualified" if qualified else "blocked",
        "alias": qualified[0]["alias"] if qualified else None,
        "requires_calibration_and_validation": True,
        "ranking_policy": "validation_combined_errors_then_p95_latency_then_peak_rss",
    }
    result = {
        "schema_version": 2,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_revision": subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip(),
        "git_dirty": bool(subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"], text=True,
            capture_output=True, check=True,
        ).stdout),
        "policy": {
            "id": ADJUDICATION_POLICY_ID,
            "protocol_version": ADJUDICATION_PROTOCOL_VERSION,
            "eligible_columns": "contiguous_primary_fallback_only",
            "protected_columns": "unanimous_and_two_of_three_majority",
            "boundary_check": "protected_neighbor_track_agreement",
        },
        "evaluations": {
            name: {
                "snapshot": snapshot_metadata[name],
                "gates": {
                    "maximum_split_errors": baselines[name],
                    "combined_must_be_less_than": sum(baselines[name].values()),
                    "identical_validated_decisions_required": True,
                    "identical_scores_required": True,
                    "zero_fallback_spans_required": True,
                },
            } for name in snapshot_paths
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
