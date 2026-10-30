#!/usr/bin/env python3
"""Turn a wp-bench ``output/results_<timestamp>.json`` into leaderboard files.

Writes one ``leaderboard/results/<model-id>.json`` per model found in the run
output. Both output shapes are accepted: a single-model run (``metadata`` +
``results``) and a multi-model run (``metadata`` + ``models``). The written
``metadata`` block is the harness metadata with the per-test ``results`` array
dropped; for multi-model runs the per-model usage summary is rebuilt from the
records the same way the harness does for single-model runs.

Example::

    python leaderboard/build_entry.py output/results_20261030_180527.json \
        --submitted-by octocat --source https://github.com/WordPress/wp-bench/pull/123
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def model_filename(model: str) -> str:
    return model.replace("/", "__") + ".json"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def usage_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Run-level usage from per-test records (mirrors wp_bench.scoring.UsageAggregator)."""
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    cost, has_cost, latencies = 0.0, False, []
    for record in records:
        usage = record.get("usage") or {}
        for key in tokens:
            if isinstance(usage.get(key), (int, float)):
                tokens[key] += int(usage[key])
        if isinstance(usage.get("cost_usd"), (int, float)):
            cost += float(usage["cost_usd"])
            has_cost = True
        if isinstance(usage.get("latency_ms"), (int, float)):
            latencies.append(float(usage["latency_ms"]))
    return {
        **tokens,
        "estimated_cost_usd": round(cost, 6) if has_cost else None,
        "median_latency_ms": round(_percentile(latencies, 0.5), 1)
        if latencies
        else None,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 1) if latencies else None,
    }


def iter_models(
    payload: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """Yield (model id, metadata, records) for every leaderboard-eligible model in a run."""
    metadata = payload["metadata"]
    if "results" in payload:
        if metadata.get("mode") != "model":
            raise SystemExit("Reference-solution runs are not leaderboard entries.")
        yield metadata["model"]["name"], metadata, payload["results"]
        return
    shared = {
        key: value
        for key, value in metadata.items()
        if key not in {"skills", "variants"}
    }
    for entry in payload["models"].values():
        if (entry.get("variant") or {}).get("kind", "none") != "none":
            continue  # skills variants are A/B diagnostics, not leaderboard rows
        records = entry["results"]
        yield (
            entry["config"]["name"],
            {
                **shared,
                "mode": "model",
                "model": entry["config"],
                "selected_test_ids": sorted(record["test_id"] for record in records),
                "errored_test_ids": sorted(
                    record["test_id"] for record in records if record.get("error")
                ),
                "usage": usage_summary(records),
                "scores": entry["scores"],
            },
            records,
        )


def build_entry(
    model: str,
    metadata: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    submitted_by: str,
    source: str,
    run_date: str | None,
    notes: str | None,
) -> dict[str, Any]:
    passed = sum(
        1 for record in records if (record.get("scores") or {}).get("execution_pass")
    )
    return {
        "metadata": {key: value for key, value in metadata.items() if key != "results"},
        "submission": {
            "submitted_by": submitted_by,
            "source": source,
            "run_date": run_date,
            "reported_at": date.today().isoformat(),
            "tests": {"passed": passed, "total": len(records)},
            "notes": notes,
        },
    }


def run_date_from_filename(path: Path) -> str | None:
    match = re.search(r"results_(\d{4})(\d{2})(\d{2})_\d{6}", path.name)
    return f"{match[1]}-{match[2]}-{match[3]}" if match else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "results", type=Path, help="output/results_<timestamp>.json from wp-bench run"
    )
    parser.add_argument(
        "--submitted-by", required=True, help="GitHub login of the person who ran it"
    )
    parser.add_argument(
        "--source", required=True, help="URL of the PR/issue/comment publishing the run"
    )
    parser.add_argument(
        "--run-date", help="ISO date of the run (default: from the results filename)"
    )
    parser.add_argument("--notes", help="Caveats worth showing next to the scores")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    payload = json.loads(args.results.read_text())
    run_date = args.run_date or run_date_from_filename(args.results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for model, metadata, records in iter_models(payload):
        entry = build_entry(
            model,
            metadata,
            records,
            submitted_by=args.submitted_by,
            source=args.source,
            run_date=run_date,
            notes=args.notes,
        )
        target = args.output_dir / model_filename(model)
        target.write_text(json.dumps(entry, indent=2) + "\n")
        print(
            f"{target}: overall {entry['metadata']['scores']['overall']:.1%} "
            f"({entry['submission']['tests']['passed']}/{entry['submission']['tests']['total']})"
        )


if __name__ == "__main__":
    main()
