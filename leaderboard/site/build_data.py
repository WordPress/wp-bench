#!/usr/bin/env python3
"""Build the leaderboard page's data file from the raw run outputs.

Reads every ``leaderboard/results/results_<timestamp>.json`` (single- or
multi-model ``wp-bench run`` output), aggregates what the page needs, and writes
``leaderboard/site/data/leaderboard.json``:

- ``tests``: id -> suite, category, source refs (shared across runs)
- ``runs``: one entry per model per run: scores, usage, latency/cost/token
  percentiles, per-category pass counts, passed test ids, failure details, and
  the commit that published the file (from ``git log``)
- ``history``: every commit that touched the results folder, with the files it
  added/removed/changed and the overall score of every model present afterwards

Run it after copying new outputs into ``leaderboard/results/`` and commit the
data file with them::

    python3 leaderboard/site/build_data.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LEADERBOARD = HERE.parent
REPO_ROOT = LEADERBOARD.parent
RESULTS_DIR = LEADERBOARD / "results"
DATA_FILE = HERE / "data" / "leaderboard.json"
RESULTS_REL = RESULTS_DIR.relative_to(REPO_ROOT).as_posix()
RUN_FILE = re.compile(r"^results_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.json$")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def usage_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Run-level usage rebuilt from records (mirrors wp_bench.scoring.UsageAggregator)."""
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
        "median_latency_ms": percentile(latencies, 0.5),
        "p95_latency_ms": percentile(latencies, 0.95),
    }


def model_entries(
    payload: dict[str, Any],
) -> list[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """(model id, run metadata, records) for every leaderboard-eligible model in a payload."""
    metadata = payload["metadata"]
    if "results" in payload:
        if metadata.get("mode", "model") != "model":
            return []
        return [(metadata["model"]["name"], metadata, payload["results"])]
    shared = {k: v for k, v in metadata.items() if k not in {"skills", "variants"}}
    entries = []
    for entry in payload.get("models", {}).values():
        if (entry.get("variant") or {}).get("kind", "none") != "none":
            continue
        records = entry["results"]
        entries.append(
            (
                entry["config"]["name"],
                {
                    **shared,
                    "mode": "model",
                    "model": entry["config"],
                    "selected_test_ids": sorted(r["test_id"] for r in records),
                    "errored_test_ids": sorted(
                        r["test_id"] for r in records if r.get("error")
                    ),
                    "usage": usage_summary(records),
                    "scores": entry["scores"],
                },
                records,
            )
        )
    return entries


def failure_detail(record: dict[str, Any]) -> dict[str, Any]:
    grader = record.get("grader") or {}
    raw = grader.get("raw") or {}
    scores = record.get("scores") or {}
    if record.get("error"):
        reason = "harness error"
    elif grader.get("timeout"):
        reason = "timeout"
    elif scores.get("static_policy_pass") is False:
        reason = "forbidden static pattern"
    elif not grader.get("success"):
        reason = "runtime crash"
    else:
        reason = "assertion failed"
    assertions = [
        a.get("description") or a.get("type") or "assertion"
        for a in ((raw.get("runtime") or {}).get("details") or {}).get("assertions", [])
        if a.get("passed") is False
    ]
    stderr = (grader.get("stderr") or "").strip()
    return {
        "reason": reason,
        "assertions": assertions[:5],
        "runtime_score": scores.get("runtime"),
        "stderr": stderr[-300:] if stderr else None,
        "error": (str(record["error"])[:300] if record.get("error") else None),
    }


def summarize(
    file: str, model: str, md: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    m = RUN_FILE.match(file)
    assert m, file
    y, mo, d, h, mi, s = m.groups()
    categories: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "total": 0}
    )
    passed_ids, failures = [], {}
    for r in records:
        ok = bool((r.get("scores") or {}).get("execution_pass"))
        categories[r["category"]]["total"] += 1
        if ok:
            categories[r["category"]]["passed"] += 1
            passed_ids.append(r["test_id"])
        else:
            failures[r["test_id"]] = failure_detail(r)
    lat = [
        r["usage"]["latency_ms"]
        for r in records
        if (r.get("usage") or {}).get("latency_ms") is not None
    ]
    cost = [
        r["usage"]["cost_usd"]
        for r in records
        if (r.get("usage") or {}).get("cost_usd") is not None
    ]
    ctok = [
        r["usage"]["completion_tokens"]
        for r in records
        if (r.get("usage") or {}).get("completion_tokens") is not None
    ]
    ptok = [
        r["usage"]["prompt_tokens"]
        for r in records
        if (r.get("usage") or {}).get("prompt_tokens") is not None
    ]
    total_cost = sum(cost) if len(cost) == len(records) and records else None
    model_cfg = md.get("model") or {}
    return {
        "id": f"{Path(file).stem}:{model}",
        "file": file,
        "model": model,
        "suite": md.get("suite"),
        "scoring_version": md.get("scoring_version"),
        "result_schema_version": md.get("result_schema_version"),
        "run_date": f"{y}-{mo}-{d}",
        "run_ts": f"{y}{mo}{d}{h}{mi}{s}",
        "config": {
            "kind": model_cfg.get("kind"),
            "temperature": model_cfg.get("temperature"),
            "max_tokens": model_cfg.get("max_tokens"),
            "runtime_image_id": md.get("runtime_image_id"),
            "runtime_isolation": md.get("runtime_isolation"),
            "runtime_isolation_boundary": md.get("runtime_isolation_boundary"),
            "execution_concurrency": md.get("execution_concurrency"),
            "seed": md.get("seed"),
            "grader": (md.get("grader") or {}).get("kind"),
            "database": (md.get("grader") or {}).get("database"),
            "dataset": md.get("dataset"),
            "continue_on_error": md.get("continue_on_error"),
        },
        "scores": md.get("scores") or {},
        "usage": md.get("usage") or {},
        "tests": {
            "passed": len(passed_ids),
            "total": len(records),
            "errored": len(md.get("errored_test_ids") or []),
        },
        "latency_ms": {
            "p50": percentile(lat, 0.5),
            "p90": percentile(lat, 0.9),
            "max": max(lat) if lat else None,
            "total_min": round(sum(lat) / 60000, 1) if lat else None,
        },
        "cost_usd": {
            "total": round(total_cost, 4) if total_cost is not None else None,
            "per_test_p50": percentile(cost, 0.5),
            "per_pass": round(total_cost / len(passed_ids), 4)
            if total_cost is not None and passed_ids
            else None,
        },
        "tokens": {
            "completion_p50": percentile(ctok, 0.5),
            "prompt_p50": percentile(ptok, 0.5),
            "completion_total": sum(ctok) if ctok else None,
        },
        "categories": dict(sorted(categories.items())),
        "passed_ids": sorted(passed_ids),
        "failures": dict(sorted(failures.items())),
        "published": None,
    }


def collect_tests(records: list[dict[str, Any]], tests: dict[str, Any]) -> None:
    for r in records:
        tests.setdefault(
            r["test_id"],
            {
                "suite": r.get("suite"),
                "category": r.get("category"),
                "source_refs": (r.get("metadata") or {}).get("source_refs") or [],
            },
        )


def folder_commits() -> list[dict[str, Any]]:
    """Commits touching the results folder, newest first, with per-file status."""
    out = git(
        "log",
        "--format=%x1e%H%x1f%h%x1f%aI%x1f%an%x1f%B",
        "--name-status",
        "--",
        RESULTS_REL,
    )
    commits = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, body = chunk.partition("\n\n")
        parts = head.split("\x1f")
        if len(parts) < 5:
            continue
        sha, short, date, author, message = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        files: dict[str, list[str]] = {"added": [], "removed": [], "modified": []}
        for line in body.splitlines():
            if not line.strip() or "\t" not in line:
                if line.strip():
                    message += "\n" + line
                continue
            status, path = line.split("\t", 1)
            name = Path(path.split("\t")[-1]).name
            if not RUN_FILE.match(name):
                continue
            key = {"A": "added", "D": "removed"}.get(status[0], "modified")
            files[key].append(name)
        if not any(files.values()):
            continue  # touched the folder, but no run output changed
        commits.append(
            {
                "sha": sha,
                "short": short,
                "date": date,
                "author": author,
                "message": message.strip(),
                **files,
            }
        )
    return commits


def snapshot(sha: str, cache: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Overall score of every model present in the folder at a commit."""
    rows: list[dict[str, Any]] = []
    for line in git("ls-tree", sha, "--", RESULTS_REL + "/").splitlines():
        meta, path = line.split("\t", 1)
        blob = meta.split()[2]
        name = Path(path).name
        if not RUN_FILE.match(name):
            continue
        if blob not in cache:
            payload = json.loads(git("show", blob))
            cache[blob] = [
                {
                    "model": model,
                    "overall": (md.get("scores") or {}).get("overall"),
                    "suite": md.get("suite"),
                    "scoring_version": md.get("scoring_version"),
                    "passed": sum(
                        1 for r in recs if (r.get("scores") or {}).get("execution_pass")
                    ),
                    "total": len(recs),
                }
                for model, md, recs in model_entries(payload)
            ]
        rows.extend({"file": name, **entry} for entry in cache[blob])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, default=DATA_FILE)
    parser.add_argument(
        "--no-history", action="store_true", help="skip git history (faster)"
    )
    args = parser.parse_args()

    tests: dict[str, Any] = {}
    runs: list[dict[str, Any]] = []
    for path in sorted(RESULTS_DIR.glob("results_*.json")):
        if not RUN_FILE.match(path.name):
            continue
        payload = json.loads(path.read_text())
        for model, md, records in model_entries(payload):
            collect_tests(records, tests)
            runs.append(summarize(path.name, model, md, records))

    history: list[dict[str, Any]] = []
    if not args.no_history:
        commits = folder_commits()
        cache: dict[str, list[dict[str, Any]]] = {}
        for c in commits:
            c["snapshot"] = snapshot(c["sha"], cache)
            history.append(c)
        last_touch: dict[str, dict[str, Any]] = {}
        for c in reversed(commits):  # oldest -> newest so the newest touch wins
            for name in c["added"] + c["modified"]:
                last_touch[name] = c
        for run in runs:
            touch = last_touch.get(run["file"])
            if touch:
                run["published"] = {
                    "by": touch["author"],
                    "date": touch["date"][:10],
                    "sha": touch["sha"],
                    "short": touch["short"],
                    "message": touch["message"].split("\n")[0],
                }

    source_commit: str | None
    try:
        source_commit = git("rev-parse", "HEAD").strip()
    except subprocess.CalledProcessError:
        source_commit = None
    data = {
        "schema": 1,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source_commit": source_commit,
        "results_dir": RESULTS_REL,
        "tests": dict(sorted(tests.items())),
        "runs": sorted(runs, key=lambda r: (r["model"], r["run_ts"])),
        "history": history,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")
    print(
        f"{args.output.relative_to(REPO_ROOT)}: {len(runs)} runs, {len(tests)} tests, {len(history)} commits, {args.output.stat().st_size // 1024} KB"
    )


if __name__ == "__main__":
    main()
