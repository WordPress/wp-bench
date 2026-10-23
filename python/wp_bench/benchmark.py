"""Frozen benchmark releases shared by the harness and leaderboard consumers.

A public version identifies the entire evaluation. Suite names, result schema
versions, and result update dates are separate provenance, never substitutes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import GraderConfig, HarnessConfig, ModelConfig
from .datasets import ExecutionTest
from .scoring import SCORING_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SCHEMA_VERSION = "1.0"
VERSION_PATTERN = re.compile(r"v[1-9][0-9]*")
# Conservative: changes to these evaluation implementations need a new release.
# CLI/output/record serialization changes alone do not change the evaluation.
HARNESS_INPUTS = (
    "artifacts.py", "config.py", "utils.py", "core.py", "datasets.py", "environment.py", "models.py",
    "scoring.py", "selection.py", "skills.py", "exploits.py",
)


def digest(value: Any) -> str:
    """Hash canonical JSON, independent of object key order."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    """Read an object with a useful error for a broken/missing manifest."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read benchmark manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Benchmark manifest must be an object: {path}")
    return value


def task_digest(test: ExecutionTest) -> str:
    """Evaluation inputs that survive the local/Hugging Face round trip.

    ``test.suite`` is a source-file ID locally and a dataset ID on the Hub;
    the enclosing run's dataset ID supplies the component identity instead.
    Reference solutions, author QA and editorial metadata are not model input.
    Raw dataset file hashes separately freeze *all* dataset changes.
    """
    return digest({name: getattr(test, name) for name in (
        "id", "prompt", "requirements", "category", "test_function",
        "static_checks", "runtime_checks", "artifact_kind",
    )})


def evaluation_snapshot(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Capture every component and its complete test membership."""
    from .datasets import _parse_execution_suite

    definition = read_json(root / "benchmark/definition.json")
    if definition.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ValueError("Unsupported benchmark definition schema")
    if definition.get("scoring_version") != SCORING_VERSION:
        raise ValueError("Working definition must use the current scoring formula")
    protocol = definition.get("protocol", {})
    ModelConfig(**protocol.get("model", {}))
    GraderConfig(**protocol.get("grader", {}))
    repeats = protocol.get("repeats")
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise ValueError("Benchmark protocol must require a positive number of repeats")
    approved = definition.get("runtime_image_ids", [])
    if not approved or len(approved) != len(set(approved)):
        raise ValueError("Benchmark must declare distinct approved runtime images")
    labels = [category["id"] for category in definition.get("categories", [])]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("Broad category IDs must be nonempty and distinct")
    suites = definition.get("suites", [])
    if not suites or len(suites) != len(set(suites)):
        raise ValueError("Benchmark must list distinct dataset suites")
    if set(definition.get("category_mapping", {})) != set(suites):
        raise ValueError("Category mappings must cover exactly the declared suites")
    files: dict[str, str] = {}
    components: dict[str, Any] = {}
    actual_suites = sorted(p.name for p in (root / "datasets/suites").iterdir() if p.is_dir())
    if sorted(suites) != actual_suites:
        raise ValueError("Benchmark definition must include every dataset suite")
    for suite in suites:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", suite):
            raise ValueError(f"Invalid dataset suite: {suite}")
        tests = []
        for path in sorted((root / "datasets/suites" / suite / "execution").glob("*.json")):
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            tests.extend(_parse_execution_suite(path))
        ids = [test.id for test in tests]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(f"Suite {suite} must contain distinct tests")
        components[suite] = {"task_count": len(tests), "tasks": {
            test.id: {"sha256": task_digest(test), "category": test.category}
            for test in sorted(tests, key=lambda test: test.id)
        }}
        # Keep public category membership exhaustive and unambiguous.
        mapping = definition["category_mapping"].get(suite, {})
        categories = {test.category for test in tests}
        allowed_labels = set(labels)
        if set(mapping) != categories or any(value not in allowed_labels for value in mapping.values()):
            raise ValueError(f"Broad category mapping is incomplete for {suite}")
    for path in sorted((root / "runtime").rglob("*")):
        if path.is_file() and (path.suffix in {".php", ".sh"} or path.name == "Dockerfile"):
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in HARNESS_INPUTS:
        path = root / "python/wp_bench" / name
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"definition": definition, "files": files, "components": components}


def load_release(version: str | None = None, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Read a frozen public release; validate its content-addressed identity."""
    catalog = read_json(root / "benchmark/releases.json")
    version = version or catalog["current"]
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Benchmark versions must be v1, v2, ...")
    entry = next((entry for entry in catalog["releases"] if entry["version"] == version), None)
    if entry is None:
        raise ValueError(f"Unknown benchmark version: {version}")
    manifest = read_json(root / "benchmark/releases" / f"{version}.json")
    if (manifest.get("schema_version") != CONTRACT_SCHEMA_VERSION
            or manifest.get("version") != version
            or manifest.get("fingerprint") != digest(manifest.get("evaluation"))
            or entry.get("fingerprint") != manifest["fingerprint"]):
        raise ValueError(f"Invalid benchmark release manifest: {version}")
    return manifest


def freeze_release(version: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Append a release, refusing to replace a previously published identity."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Benchmark versions must be v1, v2, ...")
    path = root / "benchmark/releases" / f"{version}.json"
    if path.exists():
        raise ValueError(f"Benchmark {version} already exists; published releases are immutable")
    catalog_path = root / "benchmark/releases.json"
    catalog = read_json(catalog_path) if catalog_path.exists() else {
        "schema_version": CONTRACT_SCHEMA_VERSION, "releases": [],
    }
    expected = max((int(entry["version"][1:]) for entry in catalog["releases"]), default=0) + 1
    if version != f"v{expected}":
        raise ValueError(f"Next benchmark release must be v{expected}")
    evaluation = evaluation_snapshot(root)
    if any(entry["fingerprint"] == digest(evaluation) for entry in catalog["releases"]):
        raise ValueError("Evaluation is unchanged; export updated results under the existing version")
    manifest = {"schema_version": CONTRACT_SCHEMA_VERSION, "version": version,
                "fingerprint": digest(evaluation), "evaluation": evaluation}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create protects old releases even if two release jobs race.
    with path.open("x") as handle:
        handle.write(json.dumps(manifest, indent=2) + "\n")
    catalog["releases"].append({"version": version, "path": f"releases/{version}.json",
                                "fingerprint": manifest["fingerprint"]})
    catalog["current"] = version
    catalog_path.write_text(json.dumps(catalog, indent=2) + "\n")
    return manifest


@dataclass
class BenchmarkIdentity:
    """Capture once before model calls so later disk edits cannot relabel a run."""

    manifest: dict[str, Any] | None
    fingerprint: str | None
    issues: list[str]
    suite: str
    tasks: dict[str, str]

    def record_info(self) -> dict[str, Any]:
        """Small identity repeated on streamed records, including partial runs."""
        return {"version": self.manifest["version"] if self.manifest and not self.issues else None,
                "fingerprint": self.fingerprint, "suite": self.suite,
                "status": "released" if self.manifest and not self.issues else "unreleased"}

    def bind_runtime(self, image_id: str | None, *, required: bool = False) -> None:
        """Verify the actual image before generation, rather than trusting a tag."""
        if self.manifest and image_id not in self.manifest["evaluation"]["definition"]["runtime_image_ids"]:
            self.issues.append("Runtime image is not approved for this release")
        if required and self.issues:
            raise ValueError("Cannot run as a public benchmark release: " + "; ".join(self.issues))

    def metadata(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Report exact coverage without promoting partial or errored runs."""
        info = self.record_info()
        expected = self.manifest["evaluation"]["components"].get(self.suite, {}).get("tasks", {}) if self.manifest else {}
        ids = [record["test_id"] for record in records]
        complete = bool(expected) and len(ids) == len(set(ids)) and set(ids) == set(expected)
        return {"schema_version": CONTRACT_SCHEMA_VERSION, **info,
                "based_on": self.manifest["version"] if self.manifest else None,
                "issues": self.issues, "full_suite": complete,
                "expected_task_count": len(expected), "observed_task_count": len(ids)}


def capture_identity(config: HarnessConfig, tests: list[ExecutionTest], *,
                     root: Path = PROJECT_ROOT) -> BenchmarkIdentity:
    """Identify source/test changes; unknown inputs never inherit a public label."""
    issues: list[str] = []
    tasks = {test.id: task_digest(test) for test in tests}
    manifest = None
    fingerprint = None
    try:
        manifest = load_release(config.benchmark.version, root)
        snapshot = evaluation_snapshot(root)
        fingerprint = digest(snapshot)
        if fingerprint != manifest["fingerprint"]:
            issues.append("Evaluation sources differ from the frozen release")
        component = manifest["evaluation"]["components"].get(config.run.suite, {})
        expected = component.get("tasks", {})
        if not expected or len(tasks) != len(tests) or any(
            test_id not in expected or value != expected[test_id]["sha256"]
            for test_id, value in tasks.items()
        ):
            issues.append("Loaded tasks differ from the frozen release")
        protocol = manifest["evaluation"]["definition"]["protocol"]
        if config.run.skip_static or config.run.skip_runtime or config.run.continue_on_error:
            issues.append("Diagnostic run settings are enabled")
        if config.run.execution_isolation != "reset_per_test":
            issues.append("Per-test isolation is disabled")
        if any(getattr(config.grader, key) != value for key, value in protocol["grader"].items()):
            issues.append("Grader settings differ from the release protocol")
        if not config.run.check_reference_solution and not config.run.check_exploits:
            for model in config.get_models():
                if any(getattr(model, key) != value for key, value in protocol["model"].items()):
                    issues.append(f"Model settings differ from the release protocol: {model.name}")
        if SCORING_VERSION != manifest["evaluation"]["definition"]["scoring_version"]:
            issues.append("Scoring version differs from the frozen release")
    except (ValueError, TypeError) as exc:
        issues.append(str(exc))
    if config.benchmark.version and issues:
        raise ValueError("Cannot run as " + config.benchmark.version + ": " + "; ".join(issues))
    return BenchmarkIdentity(manifest, fingerprint, issues, config.run.suite, tasks)


def publication_issues(payload: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """Validate a full-suite model result before any leaderboard ingestion.

    Receives a single-model payload. Multi-model callers adapt each models[]
    entry into this shape. No score is recomputed from a partial result.
    """
    metadata = payload.get("metadata", {})
    identity = metadata.get("benchmark") or {}
    records = payload.get("results", [])
    evaluation = manifest["evaluation"]
    definition = evaluation["definition"]
    issues = []
    if (identity.get("version") != manifest["version"]
            or identity.get("fingerprint") != manifest["fingerprint"]
            or identity.get("status") != "released"):
        issues.append("Missing or incompatible benchmark identity")
    suite = metadata.get("suite")
    expected = evaluation["components"].get(suite, {}).get("tasks", {})
    ids = [record.get("test_id") for record in records]
    if not expected or len(ids) != len(set(ids)) or set(ids) != set(expected):
        issues.append("Incomplete, duplicate, or unknown suite tasks")
    if metadata.get("scoring_version") != definition["scoring_version"]:
        issues.append("Scoring version mismatch")
    if metadata.get("runtime_image_id") not in definition["runtime_image_ids"]:
        issues.append("Runtime image is not approved for this release")
    grader = metadata.get("grader", {})
    if any(grader.get(key) != value for key, value in definition["protocol"]["grader"].items()):
        issues.append("Grader settings differ from the release protocol")
    if metadata.get("runtime_isolation") != "reset_per_test":
        issues.append("Run did not use per-test isolation")
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        issues.append("Missing run identity")
    for record in records:
        if record.get("run_id") != run_id:
            issues.append("Record run identity mismatch")
        task = expected.get(record.get("test_id"), {})
        if (record.get("category") != task.get("category")
                or (record.get("benchmark") or {}).get("task_sha256") != task.get("sha256")):
            issues.append("Record task fingerprint mismatch")
        model = record.get("model") or {}
        if (record.get("mode") != "model" or record.get("error") is not None
                or (record.get("model_call") or {}).get("temperature_fallback", False)
                or record.get("variant", {}).get("kind") != "none"
                or not isinstance(record.get("scores", {}).get("execution_pass"), bool)):
            issues.append("Diagnostic, errored, or ungraded record")
        if any(model.get(key) != value for key, value in definition["protocol"]["model"].items()):
            issues.append("Model settings differ from the release protocol")
        record_identity = record.get("benchmark") or {}
        if (record_identity.get("version") != manifest["version"]
                or record_identity.get("fingerprint") != manifest["fingerprint"]
                or record_identity.get("suite") != suite
                or record_identity.get("status") != "released"):
            issues.append("Record benchmark identity mismatch")
    return list(dict.fromkeys(issues))
