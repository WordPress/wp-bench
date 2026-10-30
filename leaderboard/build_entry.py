#!/usr/bin/env python3
"""Copy a wp-bench ``output/results_<timestamp>.json`` into ``leaderboard/results/``.

One ``<model-id>.json`` is written per model, in the exact shape ``wp-bench run``
writes for a single-model run (``metadata`` + per-test ``results``). A
single-model output is copied verbatim. A multi-model output (``metadata`` +
``models``) is split into one single-model payload per model: the shared run
metadata plus that model's config, scores, test ids and a usage summary rebuilt
from its records the same way the harness does. Skills variants are skipped.

Example::

    python leaderboard/build_entry.py output/results_20261030_180527.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
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


def iter_payloads(payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (model id, single-model payload) for every leaderboard-eligible model in a run."""
    metadata = payload["metadata"]
    if "results" in payload:
        if metadata.get("mode") != "model":
            raise SystemExit("Reference-solution runs are not leaderboard entries.")
        yield metadata["model"]["name"], payload
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
                "metadata": {
                    **shared,
                    "mode": "model",
                    "model": entry["config"],
                    "selected_test_ids": sorted(
                        record["test_id"] for record in records
                    ),
                    "errored_test_ids": sorted(
                        record["test_id"] for record in records if record.get("error")
                    ),
                    "usage": usage_summary(records),
                    "scores": entry["scores"],
                },
                "results": records,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "results", type=Path, help="output/results_<timestamp>.json from wp-bench run"
    )
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    payload = json.loads(args.results.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for model, entry in iter_payloads(payload):
        target = args.output_dir / model_filename(model)
        target.write_text(json.dumps(entry, indent=2) + "\n")
        scores = entry["metadata"]["scores"]
        print(
            f"{target}: overall {scores['overall']:.1%} over {len(entry['results'])} tests"
        )


if __name__ == "__main__":
    main()
