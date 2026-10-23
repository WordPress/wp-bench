"""Adapt validated WP-Bench exports to the public site's display data.

No model calls or campaign-specific paths. Public versions and categories come
from the repository release; labels and generation pairs are editorial inputs.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from wp_bench.benchmark import digest, load_release, read_json


def interval(values: list[float]) -> list[float]:
    array = np.array(values, dtype=float)
    rng = np.random.default_rng(1337)
    samples = array[rng.integers(0, len(array), size=(10000, len(array)))].mean(axis=1)
    return [float(value) for value in np.quantile(samples, [0.025, 0.975])]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        grouped.setdefault((row["suite"], row["test_id"]), []).append(float(row["passed"]))
    latencies = [row["usage"]["latency_ms"] / 1000 for row in rows if row["usage"].get("latency_ms") is not None]
    costs = [row["usage"]["cost_usd"] for row in rows if row["usage"].get("cost_usd") is not None]
    passed = sum(row["passed"] for row in rows)
    return {"passed": passed, "total": len(rows), "rate": passed / len(rows),
            "ci": interval([statistics.mean(values) for _, values in sorted(grouped.items())]),
            "median_seconds": statistics.median(latencies) if latencies else None,
            "cost_per_100": 100 * statistics.mean(costs) if len(costs) == len(rows) else None}


def build_data(bundle: dict[str, Any], manifest: dict[str, Any], catalog: dict[str, Any],
               source_commit: str) -> dict[str, Any]:
    """Check identities and coverage before deriving any displayed scores."""
    content = {key: value for key, value in bundle.items() if key not in {"snapshot_id", "fingerprint", "updated_at"}}
    if (bundle.get("schema_version") != "1.0" or digest(content) != bundle.get("fingerprint")
            or bundle.get("benchmark") != {"version": manifest["version"], "fingerprint": manifest["fingerprint"]}):
        raise ValueError("Export and frozen benchmark identities differ")
    definition = manifest["evaluation"]["definition"]
    tasks = {(suite, test_id): task for suite, component in manifest["evaluation"]["components"].items()
             for test_id, task in component["tasks"].items()}
    categories = [{**category, "task_count": sum(
        definition["category_mapping"][suite][task["category"]] == category["id"]
        for (suite, _), task in tasks.items())} for category in definition["categories"]]
    categories = [category for category in categories if category["task_count"]]
    models: list[dict[str, Any]] = []
    means: dict[str, dict[tuple[str, str], float]] = {}
    flat: list[dict[str, Any]] = []
    seen_models = set()
    for model in bundle["models"]:
        name, rows = model["id"], model["answers"]
        if name in seen_models:
            raise ValueError("Duplicate model identity")
        seen_models.add(name)
        grouped: dict[tuple[str, str], list[float]] = {}
        run_tasks: dict[tuple[str, str], set[str]] = {}
        seen_rows = set()
        for row in rows:
            key = row["suite"], row["test_id"]
            row_key = (*key, row["run_id"])
            if row_key in seen_rows or key not in tasks or not isinstance(row["passed"], bool):
                raise ValueError("Duplicate, unknown, or ungraded task")
            seen_rows.add(row_key)
            expected_category = definition["category_mapping"][key[0]][tasks[key]["category"]]
            if row["category"] != expected_category:
                raise ValueError("Category differs from the release mapping")
            grouped.setdefault(key, []).append(float(row["passed"]))
            run_tasks.setdefault((row["suite"], row["run_id"]), set()).add(row["test_id"])
        repeats = definition["protocol"]["repeats"]
        if set(grouped) != set(tasks) or any(len(values) != repeats for values in grouped.values()):
            raise ValueError("Model lacks complete, equal repeated task coverage")
        for (suite, _), ids in run_tasks.items():
            if ids != set(manifest["evaluation"]["components"][suite]["tasks"]):
                raise ValueError("A repeat contains only a partial suite")
        meta = catalog.get("models", {}).get(name, {})
        provider = meta.get("provider") or ("Google" if name.startswith("gemini/") else "Anthropic" if name.startswith("anthropic/") else "OpenAI" if name.startswith(("gpt-", "openai/", "o1", "o3", "o4")) else "Other")
        models.append({"id": name, "label": meta.get("label", name), "provider": provider,
                       "generation": meta.get("generation", "Evaluated"), "repeats": repeats,
                       "overall": summarize(rows), "categories": {
                           category["id"]: summarize([row for row in rows if row["category"] == category["id"]])
                           for category in categories if category["task_count"]}})
        means[name] = {key: statistics.mean(values) for key, values in grouped.items()}
        flat.extend({"model": name, **row} for row in rows)
    if not models:
        raise ValueError("Export has no models")
    comparisons = []
    for pair in catalog.get("pairs", []):
        previous, current = pair["previous"], pair["current"]
        if previous not in means or current not in means:
            continue
        differences = [means[current][key] - means[previous][key] for key in sorted(tasks)]
        delta, ci = 100 * statistics.mean(differences), [100 * value for value in interval(differences)]
        comparisons.append({**pair, "delta_pp": delta, "ci_pp": ci,
                            "meaningful_improvement": delta >= 3 and ci[0] > 0,
                            "interpretation": "Improvement across these tasks" if ci[0] > 0 else
                            "Regression across these tasks" if ci[1] < 0 else "Difference is uncertain on this task set"})
    gains = sum(pair["delta_pp"] > 0 for pair in comparisons)
    meaningful = sum(pair["meaningful_improvement"] for pair in comparisons)
    finding = (f"Newer models scored higher in {gains} of {len(comparisons)} matched comparisons. "
               f"{meaningful} gains were at least 3 percentage points with a positive paired interval. "
               "Those pairs show meaningful gains on this task set; progress varies across model families.") if meaningful else (
               "This release does not establish meaningful improvement across model generations. Use the measured category results to choose a model.")
    date = datetime.fromisoformat(bundle["updated_at"])
    if date.tzinfo is None:
        raise ValueError("Export update time must include a timezone")
    date = date.astimezone(timezone.utc)
    result = {"data_schema_version": "1.0", "updated_at": date.isoformat(), "cost_estimate_basis": "recorded_usage", "display_date": date.strftime("%B %d, %Y").replace(" 0", " "),
              "benchmark": bundle["benchmark"], "commit": source_commit,
              "dataset_sha256": digest({path: value for path, value in manifest["evaluation"]["files"].items() if path.startswith("datasets/")}),
              "wordpress_version": definition["wordpress_version"], "scoring_version": definition["scoring_version"],
              "task_count": len(tasks), "core_task_count": len(manifest["evaluation"]["components"].get("wp-core", {}).get("tasks", {})),
              "cli_task_count": len(manifest["evaluation"]["components"].get("wp-cli", {}).get("tasks", {})),
              "categories": categories, "models": sorted(models, key=lambda model: (-model["overall"]["rate"], model["label"])),
              "comparisons": comparisons, "finding": finding,
              "methodology": {"settings": f"Frozen {manifest['version']} protocol. First response, baseline without skills; provider default reasoning. Generation and grader settings are recorded in the release definition.",
                              "limitations": "A fixed public task set cannot represent every WordPress project. Training exposure is unknown. Category sizes vary. Latency and cost estimates depend on provider behavior and API concurrency.",
                              "cost_note": "Estimated costs use the usage recorded by the harness. Actual billing may differ.",
                              "comparison_note": "Generation deltas use identical tasks and 95% paired bootstrap intervals (10,000 resamples, seed 1337). Intervals are exploratory and not adjusted for multiple comparisons. A meaningful gain is at least 3 percentage points with a positive interval."},
              "coverage_note": "Strict behavioral pass rate. Ties share a rank. Category scores use only that category’s tasks. Small gaps may not be meaningful.",
              "audit_files": []}
    result["data_version"] = date.strftime("%Y.%m.%d.") + digest({"results": result, "export_fingerprint": bundle["fingerprint"]})[:8]
    return {"results": result, "manifest": {"benchmark": bundle["benchmark"], "export_fingerprint": bundle["fingerprint"],
                                             "input_sha256": bundle["input_sha256"], "source_commit": source_commit}, "answers": flat}


def write_data(data: dict[str, Any], destination: Path) -> Path:
    """Keep version-scoped display files immutable and refresh the latest alias."""
    version = data["results"]["data_version"]
    release = destination / version
    body = json.dumps(data["results"], indent=2, allow_nan=False) + "\n"
    manifest = json.dumps({"data_version": version, **data["manifest"]}, indent=2) + "\n"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["model", "suite", "test_id", "run_id", "category", "passed", "latency_ms", "cost_usd"])
    writer.writeheader()
    for answer in data["answers"]:
        writer.writerow({**{key: answer[key] for key in ["model", "suite", "test_id", "run_id", "category", "passed"]},
                         "latency_ms": answer["usage"].get("latency_ms"), "cost_usd": answer["usage"].get("cost_usd")})
    files = {"results.json": body, "manifest.json": manifest, "task-results.csv": stream.getvalue()}
    for name, content in files.items():
        path = release / name
        if path.exists() and path.read_bytes() != content.encode():
            raise ValueError("An immutable display-data release would be overwritten")
    release.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (release / name).write_bytes(content.encode())
    (destination / "results.json").write_text(body)
    return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True, help="Editorial model labels and generation pairs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    bundle = read_json(args.export)
    data = build_data(bundle, load_release(bundle["benchmark"]["version"]), read_json(args.catalog), args.source_commit)
    print(json.dumps({"path": str(write_data(data, args.output_dir)), "benchmark": bundle["benchmark"]}))


if __name__ == "__main__":
    main()
