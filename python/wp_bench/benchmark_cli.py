"""Release and leaderboard commands; no provider calls or runtime startup."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from .benchmark import (
    CONTRACT_SCHEMA_VERSION,
    PROJECT_ROOT,
    digest,
    evaluation_snapshot,
    freeze_release,
    load_release,
    publication_issues,
    read_json,
)

app = typer.Typer(help="Inspect and freeze public benchmark releases; export validated results.")


def check_releases(root: Path = PROJECT_ROOT, base_ref: str | None = None) -> dict[str, Any]:
    """Validate the catalog and protect all releases already present upstream."""
    catalog = read_json(root / "benchmark/releases.json")
    if catalog.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ValueError("Unsupported benchmark catalog schema")
    entries = catalog.get("releases", [])
    versions = [entry["version"] for entry in entries]
    if not versions or versions != [f"v{number}" for number in range(1, len(versions) + 1)]:
        raise ValueError("Release catalog must contain v1, v2, ... exactly once, in order")
    if catalog.get("current") != versions[-1]:
        raise ValueError("Current benchmark must be the latest frozen release")
    if {path.stem for path in (root / "benchmark/releases").glob("*.json")} != set(versions):
        raise ValueError("Release files and catalog differ")
    for entry in entries:
        if entry.get("path") != f"releases/{entry['version']}.json":
            raise ValueError("Unexpected release manifest path")
        load_release(entry["version"], root)
    latest = load_release(root=root)
    matches = digest(evaluation_snapshot(root)) == latest["fingerprint"]
    if base_ref:
        def git(*args: str) -> bytes:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise ValueError("Cannot inspect benchmark release history: " + result.stderr.decode().strip())
            return result.stdout
        base_files = git("ls-tree", "-r", "--name-only", base_ref, "--", "benchmark/releases").decode().splitlines()
        for relative in base_files:
            path = root / relative
            if not path.is_file() or path.read_bytes() != git("show", f"{base_ref}:{relative}"):
                raise ValueError(f"Published benchmark manifest changed or disappeared: {relative}")
        if f"benchmark/releases/{latest['version']}.json" not in base_files and not matches:
            raise ValueError("New release does not describe the current evaluation; freeze again before publishing")
    return {"current": latest["version"], "fingerprint": latest["fingerprint"],
            "working_tree": "released" if matches else "unreleased", "releases": versions}


def export_results(paths: list[Path], output: Path, *, version: str | None = None,
                   root: Path = PROJECT_ROOT) -> Path:
    """Publish an immutable, version-scoped scored-answer bundle for websites.

    Require complete suites and the same number of independent runs for every
    model. Preserve old snapshots. Consumers derive displays from these rows;
    no provider's self-reported aggregate can replace the actual task results.
    """
    manifest = load_release(version, root)
    definition = manifest["evaluation"]["definition"]
    models: dict[str, list[dict[str, Any]]] = {}
    runs: dict[tuple[str, str], set[str]] = {}
    used_runs: set[str] = set()
    inputs: list[str] = []
    for path in paths:
        if path.name.endswith((".partial", ".jsonl")):
            raise ValueError(f"Expected completed results JSON, not a stream: {path}")
        payload = read_json(path)
        inputs.append(digest(payload))
        entries = [payload]
        if "models" in payload:
            entries = [{"metadata": {**payload["metadata"], "run_id": result["run_id"],
                                      "benchmark": result["benchmark"]}, "results": result["results"]}
                       for result in payload["models"].values()]
        for result in entries:
            issues = publication_issues(result, manifest)
            if issues:
                raise ValueError(f"Cannot publish {path.name}: {'; '.join(issues)}")
            records = result["results"]
            names = {record["model"]["name"] for record in records}
            if len(names) != 1:
                raise ValueError("A model run must contain exactly one model identity")
            name = names.pop()
            metadata = result["metadata"]
            suite, run_id = metadata["suite"], metadata["run_id"]
            if run_id in used_runs:
                raise ValueError("Duplicate run identity; the same run cannot count as another repeat")
            used_runs.add(run_id)
            runs.setdefault((name, suite), set()).add(run_id)
            answers = models.setdefault(name, [])
            for record in records:
                task = manifest["evaluation"]["components"][suite]["tasks"][record["test_id"]]
                answers.append({"suite": suite, "test_id": record["test_id"], "run_id": run_id,
                                "category": definition["category_mapping"][suite][task["category"]],
                                "passed": record["scores"]["execution_pass"],
                                "usage": {key: (record.get("usage") or {}).get(key) for key in (
                                    "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "latency_ms")}})
    if not models:
        raise ValueError("No completed model results supplied")
    repeats = definition["protocol"]["repeats"]
    for name in models:
        for suite in definition["suites"]:
            if len(runs.get((name, suite), set())) != repeats:
                raise ValueError(f"{name} needs exactly {repeats} independent complete runs of {suite}")
    content: dict[str, Any] = {"schema_version": CONTRACT_SCHEMA_VERSION,
               "benchmark": {"version": manifest["version"], "fingerprint": manifest["fingerprint"]},
               "categories": definition["categories"],
               "models": [{"id": name, "answers": sorted(answers, key=lambda answer: (
                   answer["suite"], answer["test_id"], answer["run_id"]))}
                   for name, answers in sorted(models.items())], "input_sha256": sorted(inputs)}
    fingerprint = digest(content)
    snapshot_id = fingerprint[:16]
    directory = output / manifest["version"]
    destination = directory / f"{snapshot_id}.json"
    catalog_path = output / "results.json"
    catalog: dict[str, Any] = read_json(catalog_path) if catalog_path.exists() else {
        "schema_version": CONTRACT_SCHEMA_VERSION, "benchmarks": {},
    }
    # Same exact inputs preserve both their date and the latest-results pointer.
    created = not destination.exists()
    if not created:
        existing = read_json(destination)
        existing_content = {key: value for key, value in existing.items()
                            if key not in {"snapshot_id", "fingerprint", "updated_at"}}
        if existing.get("fingerprint") != fingerprint or digest(existing_content) != fingerprint:
            raise ValueError("Existing result snapshot has a different fingerprint")
    else:
        snapshot = {**content, "snapshot_id": snapshot_id, "fingerprint": fingerprint,
                    "updated_at": datetime.now(timezone.utc).isoformat()}
        directory.mkdir(parents=True, exist_ok=True)
        with destination.open("x") as handle:
            handle.write(json.dumps(snapshot, indent=2, allow_nan=False) + "\n")
    entry = catalog["benchmarks"].setdefault(manifest["version"], {"current": snapshot_id, "snapshots": []})
    if snapshot_id not in [item["id"] for item in entry["snapshots"]]:
        entry["snapshots"].append({"id": snapshot_id, "path": f"{manifest['version']}/{snapshot_id}.json"})
    if created:
        entry["current"] = snapshot_id
    temporary = catalog_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(catalog, indent=2) + "\n")
    temporary.replace(catalog_path)
    return destination


def _show(action: Any) -> None:
    try:
        result = action()
    except (ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("show")
def show(version: str | None = typer.Option(None, help="Public release, such as v1")) -> None:
    """Print the complete machine-readable release manifest."""
    _show(lambda: load_release(version))


@app.command("check")
def check(base_ref: str | None = typer.Option(None, help="Git commit whose published releases must remain immutable")) -> None:
    """Validate releases; report changed evaluation sources as unreleased."""
    _show(lambda: check_releases(base_ref=base_ref))


@app.command("freeze")
def freeze(version: str = typer.Argument(..., help="Next public version, e.g. v2")) -> None:
    """Freeze the entire current evaluation as the next immutable release."""
    _show(lambda: {key: value for key, value in freeze_release(version).items() if key != "evaluation"})


@app.command("export")
def export(inputs: list[Path] = typer.Option(..., "--input", help="Completed result JSON; repeat for every suite and run"),
           output: Path = typer.Option(..., "--output-dir", help="Archive root; keeps snapshots for every benchmark version"),
           version: str | None = typer.Option(None, help="Public release, defaults to latest")) -> None:
    """Validate results and write an immutable leaderboard data snapshot."""
    _show(lambda: {"path": str(export_results(inputs, output, version=version))})
