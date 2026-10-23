"""Release identity, immutable history, and safe website ingestion."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from wp_bench.benchmark import (
    HARNESS_INPUTS,
    capture_identity,
    digest,
    evaluation_snapshot,
    freeze_release,
    load_release,
    publication_issues,
    read_json,
)
from wp_bench.benchmark_cli import app, check_releases, export_results
from wp_bench.config import (
    BenchmarkConfig,
    DatasetConfig,
    HarnessConfig,
    ModelConfig,
    RunConfig,
)
from wp_bench.datasets import _parse_execution_suite


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    definition = {
        "schema_version": "1.0", "wordpress_version": "7.1", "scoring_version": "3.0",
        "suites": ["wp-core", "wp-cli"],
        "categories": [{"id": "core", "label": "Core"}, {"id": "wp-cli", "label": "WP-CLI"}],
        "category_mapping": {"wp-core": {"hooks": "core"}, "wp-cli": {"cli": "wp-cli"}},
        "protocol": {"model": {"temperature": 1.0, "top_p": None, "max_tokens": 16384},
                     "grader": {"kind": "docker", "timeout_seconds": 90}, "repeats": 2},
        "runtime_image_ids": ["sha256:approved"],
    }
    (root / "benchmark").mkdir(parents=True)
    (root / "benchmark/definition.json").write_text(json.dumps(definition))
    for suite, category in [("wp-core", "hooks"), ("wp-cli", "cli")]:
        directory = root / "datasets/suites" / suite / "execution"
        directory.mkdir(parents=True)
        (directory / "tasks.json").write_text(json.dumps({"id": suite, "tests": [{
            "id": f"e-{category}-001", "prompt": "Implement the requested WordPress behavior.",
            "requirements": [], "category": category, "runtime_checks": {"assertions": []},
        }]}))
    (root / "runtime").mkdir()
    (root / "runtime/verify-runtime.php").write_text("<?php // trusted grader\n")
    (root / "python/wp_bench").mkdir(parents=True)
    for name in HARNESS_INPUTS:
        (root / "python/wp_bench" / name).write_text("# fixture\n")
    freeze_release("v1", root)
    return root


def config(suite: str = "wp-core", pinned: bool = False) -> HarnessConfig:
    return HarnessConfig(
        benchmark=BenchmarkConfig(version="v1" if pinned else None),
        dataset=DatasetConfig(source="local", name=suite),
        model=ModelConfig(name="model-a", temperature=1, max_tokens=16384),
        run=RunConfig(suite=suite),
    )


def result(root: Path, suite: str, run_id: str, name: str = "model-a") -> dict[str, Any]:
    cfg = config(suite)
    tests = _parse_execution_suite(root / "datasets/suites" / suite / "execution/tasks.json")
    identity = capture_identity(cfg, tests, root=root)
    identity.bind_runtime("sha256:approved")
    records = [{"test_id": test.id, "category": test.category, "mode": "model",
                "model": {"name": name, "temperature": 1.0, "top_p": None, "max_tokens": 16384},
                "variant": {"kind": "none"}, "scores": {"execution_pass": True},
                "error": None, "run_id": run_id,
                "benchmark": {**identity.record_info(), "task_sha256": identity.tasks[test.id]}}
               for test in tests]
    return {"metadata": {"suite": suite, "run_id": run_id, "benchmark": identity.metadata(records),
                         "grader": cfg.grader.model_dump(mode="json"), "scoring_version": "3.0",
                         "runtime_isolation": "reset_per_test", "runtime_image_id": "sha256:approved"},
            "results": records}


def inputs(root: Path, tmp_path: Path, name: str = "model-a") -> list[Path]:
    paths = []
    for suite in ["wp-core", "wp-cli"]:
        for repeat in [1, 2]:
            path = tmp_path / f"{name}-{suite}-{repeat}.json"
            path.write_text(json.dumps(result(root, suite, f"{name}-{suite}-{repeat}", name)))
            paths.append(path)
    return paths


def test_one_version_freezes_every_dataset_and_the_runtime(release_root: Path) -> None:
    manifest = load_release(root=release_root)
    assert manifest["version"] == "v1"
    assert set(manifest["evaluation"]["components"]) == {"wp-core", "wp-cli"}
    assert manifest["fingerprint"] == digest(evaluation_snapshot(release_root))
    assert check_releases(release_root)["working_tree"] == "released"


@pytest.mark.parametrize("relative", [
    "datasets/suites/wp-core/execution/tasks.json", "datasets/suites/wp-cli/execution/tasks.json",
    "runtime/verify-runtime.php", "python/wp_bench/scoring.py", "benchmark/definition.json",
])
def test_any_component_change_requires_a_new_release(release_root: Path, relative: str) -> None:
    path = release_root / relative
    if relative == "benchmark/definition.json":
        definition = read_json(path)
        definition["protocol"]["grader"]["timeout_seconds"] = 120
        path.write_text(json.dumps(definition))
    else:
        path.write_text(path.read_text() + "\n")
    # JSON formatting is conservatively part of a frozen source snapshot too.
    tests = _parse_execution_suite(release_root / "datasets/suites/wp-core/execution/tasks.json")
    identity = capture_identity(config(), tests, root=release_root)
    assert identity.record_info()["version"] is None
    assert identity.record_info()["status"] == "unreleased"
    with pytest.raises(ValueError, match="Cannot run as v1"):
        capture_identity(config(pinned=True), tests, root=release_root)
    old = load_release(root=release_root)
    new = freeze_release("v2", release_root)
    assert old["fingerprint"] != new["fingerprint"]
    assert load_release("v1", release_root) == old


def test_model_additions_do_not_create_a_benchmark_version(release_root: Path) -> None:
    before = load_release(root=release_root)
    cfg = config()
    cfg.model = ModelConfig(name="another-model", temperature=1, max_tokens=16384)
    tests = _parse_execution_suite(release_root / "datasets/suites/wp-core/execution/tasks.json")
    assert capture_identity(cfg, tests, root=release_root).record_info()["version"] == "v1"
    assert load_release(root=release_root) == before
    with pytest.raises(ValueError, match="unchanged"):
        freeze_release("v2", release_root)


def test_changed_loaded_hub_task_cannot_claim_a_release(release_root: Path) -> None:
    tests = _parse_execution_suite(release_root / "datasets/suites/wp-core/execution/tasks.json")
    tests[0].prompt = "A different prompt, from an unpinned Hub revision"
    with pytest.raises(ValueError, match="Loaded tasks differ"):
        capture_identity(config(pinned=True), tests, root=release_root)


def test_model_protocol_change_is_not_v1(release_root: Path) -> None:
    cfg = config(pinned=True)
    assert cfg.model is not None
    cfg.model.temperature = 0
    tests = _parse_execution_suite(release_root / "datasets/suites/wp-core/execution/tasks.json")
    with pytest.raises(ValueError, match="Model settings"):
        capture_identity(cfg, tests, root=release_root)


def test_runtime_is_verified_before_pinned_generation(release_root: Path) -> None:
    tests = _parse_execution_suite(release_root / "datasets/suites/wp-core/execution/tasks.json")
    identity = capture_identity(config(pinned=True), tests, root=release_root)
    with pytest.raises(ValueError, match="Runtime image"):
        identity.bind_runtime("sha256:unapproved", required=True)


def test_frozen_releases_cannot_be_overwritten_or_relabelled(release_root: Path) -> None:
    path = release_root / "benchmark/releases/v1.json"
    original = path.read_bytes()
    with pytest.raises(ValueError, match="immutable"):
        freeze_release("v1", release_root)
    with pytest.raises(ValueError, match="Next benchmark release must be v2"):
        freeze_release("v3", release_root)
    assert path.read_bytes() == original
    changed = json.loads(original)
    changed["evaluation"]["definition"]["wordpress_version"] = "another-version"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="Invalid benchmark release"):
        load_release(root=release_root)


@pytest.mark.parametrize("change", ["partial", "duplicate", "error", "reference", "skills", "mixed-version",
                                    "wrong-image", "wrong-grader", "wrong-task", "wrong-run", "ungraded"])
def test_unsafe_results_cannot_enter_a_leaderboard(release_root: Path, change: str) -> None:
    payload = result(release_root, "wp-core", "run-one")
    assert publication_issues(payload, load_release(root=release_root)) == []
    record = payload["results"][0]
    if change == "partial":
        payload["results"] = []
    elif change == "duplicate":
        payload["results"].append(copy.deepcopy(record))
    elif change == "error":
        record["error"] = {"type": "provider_error"}
    elif change == "reference":
        record["mode"] = "reference_solution"
    elif change == "skills":
        record["variant"]["kind"] = "skills"
    elif change == "mixed-version":
        record["benchmark"]["version"] = "v2"
    elif change == "wrong-image":
        payload["metadata"]["runtime_image_id"] = "sha256:another"
    elif change == "wrong-grader":
        payload["metadata"]["grader"]["timeout_seconds"] = 120
    elif change == "wrong-task":
        record["benchmark"]["task_sha256"] = "different"
    elif change == "wrong-run":
        record["run_id"] = "another-run"
    elif change == "ungraded":
        record["scores"]["execution_pass"] = None
    assert publication_issues(payload, load_release(root=release_root))


def test_export_preserves_old_snapshots_and_is_idempotent(release_root: Path, tmp_path: Path) -> None:
    paths = inputs(release_root, tmp_path)
    output = tmp_path / "public"
    first = export_results(paths, output, root=release_root)
    original = first.read_bytes()
    assert export_results(paths, output, root=release_root) == first
    assert first.read_bytes() == original
    bundle = json.loads(original)
    assert bundle["benchmark"]["version"] == "v1"
    assert len(bundle["models"][0]["answers"]) == 4
    second = export_results(paths + inputs(release_root, tmp_path, "model-b"), output, root=release_root)
    assert second != first
    assert first.read_bytes() == original
    catalog = read_json(output / "results.json")
    assert len(catalog["benchmarks"]["v1"]["snapshots"]) == 2
    assert export_results(paths, output, root=release_root) == first
    assert read_json(output / "results.json") == catalog


def test_export_requires_both_complete_suites_and_independent_repeats(release_root: Path, tmp_path: Path) -> None:
    paths = inputs(release_root, tmp_path)
    with pytest.raises(ValueError, match="independent complete runs"):
        export_results(paths[:-1], tmp_path / "public", root=release_root)
    with pytest.raises(ValueError, match="Duplicate run identity"):
        export_results(paths + [paths[0]], tmp_path / "public", root=release_root)
    assert not (tmp_path / "public").exists()


def test_export_supports_multi_model_result_envelopes(release_root: Path, tmp_path: Path) -> None:
    paths = inputs(release_root, tmp_path)
    for path in paths:
        payload = read_json(path)
        metadata = payload["metadata"]
        wrapped = {"metadata": metadata, "models": {"model-a": {
            "run_id": metadata["run_id"], "benchmark": metadata["benchmark"], "results": payload["results"],
        }}}
        path.write_text(json.dumps(wrapped))
    destination = export_results(paths, tmp_path / "public", root=release_root)
    assert len(read_json(destination)["models"]) == 1


def test_ci_detects_changes_even_when_manifest_hashes_are_recomputed(release_root: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=release_root, check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
    base = git("rev-parse", "HEAD")
    assert check_releases(release_root, base)["working_tree"] == "released"
    path = release_root / "benchmark/releases/v1.json"
    manifest = read_json(path)
    manifest["evaluation"]["definition"]["wordpress_version"] = "changed"
    manifest["fingerprint"] = digest(manifest["evaluation"])
    path.write_text(json.dumps(manifest))
    catalog_path = release_root / "benchmark/releases.json"
    catalog = read_json(catalog_path)
    catalog["releases"][0]["fingerprint"] = manifest["fingerprint"]
    catalog_path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match="Published benchmark manifest changed"):
        check_releases(release_root, base)


def test_cli_exposes_json_contract_without_a_model_call(release_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wp_bench.benchmark_cli.load_release", lambda version=None: load_release(version, release_root))
    response = CliRunner().invoke(app, ["show", "--version", "v1"])
    assert response.exit_code == 0
    assert json.loads(response.stdout)["version"] == "v1"



def test_export_refuses_a_corrupted_existing_snapshot(release_root: Path, tmp_path: Path) -> None:
    paths = inputs(release_root, tmp_path)
    output = tmp_path / "public"
    destination = export_results(paths, output, root=release_root)
    snapshot = read_json(destination)
    snapshot["models"][0]["answers"][0]["passed"] = False
    destination.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError, match="different fingerprint"):
        export_results(paths, output, root=release_root)


@pytest.mark.parametrize("field", ["repeats", "images", "categories", "mapping", "temperature"])
def test_freeze_rejects_an_invalid_recipe(release_root: Path, field: str) -> None:
    path = release_root / "benchmark/definition.json"
    definition = read_json(path)
    if field == "repeats":
        definition["protocol"]["repeats"] = 0
    elif field == "images":
        definition["runtime_image_ids"] = []
    elif field == "categories":
        definition["categories"].append(definition["categories"][0])
    elif field == "mapping":
        del definition["category_mapping"]["wp-cli"]
    elif field == "temperature":
        definition["protocol"]["model"]["temperature"] = 20
    path.write_text(json.dumps(definition))
    with pytest.raises(ValueError):
        freeze_release("v2", release_root)
    assert not (release_root / "benchmark/releases/v2.json").exists()


def test_harness_streams_the_same_verified_identity_as_the_completed_payload(
    release_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import fake_generation

    from wp_bench.config import OutputConfig
    from wp_bench.core import BenchmarkRunner
    from wp_bench.environment import ExecutionResult

    cfg = config(pinned=True)
    cfg.output = OutputConfig(path=tmp_path / "complete.json", jsonl_path=tmp_path / "stream.jsonl")
    tests = _parse_execution_suite(release_root / "datasets/suites/wp-core/execution/tasks.json")
    monkeypatch.setattr("wp_bench.core.load_tests", lambda config: tests)
    monkeypatch.setattr("wp_bench.core.capture_identity", lambda config, tests: capture_identity(config, tests, root=release_root))

    class Environment:
        image_id = "sha256:approved"

        def setup(self, **kwargs: Any) -> None: ...
        def reset(self) -> None: ...
        def release(self) -> None: ...
        def close(self) -> None: ...
        def execute_artifact(self, artifact: Any, spec: Any) -> ExecutionResult:
            return ExecutionResult(success=True, raw={"success": True, "runtime": {"score": 1.0}, "static": {"score": 1.0}}, stdout="", stderr="")

    runner = BenchmarkRunner(cfg)
    runner.environment = Environment()  # type: ignore[assignment]
    monkeypatch.setattr(runner.model, "generate_with_metadata", lambda prompt: fake_generation("<?php echo 1;"))
    payload = runner.run()
    assert payload["metadata"]["benchmark"]["version"] == "v1"
    assert payload["metadata"]["benchmark"]["full_suite"] is True
    streamed = [json.loads(line) for line in next(tmp_path.glob("stream_*.jsonl")).read_text().splitlines()]
    assert streamed == payload["results"]
    assert publication_issues(payload, load_release(root=release_root)) == []
    assert streamed[0]["run_id"] == payload["metadata"]["run_id"]


def _website_adapter() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "web/build_results.py"
    spec = importlib.util.spec_from_file_location("leaderboard_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_website_builds_from_the_shared_export_and_release(release_root: Path, tmp_path: Path) -> None:
    exported = export_results(inputs(release_root, tmp_path), tmp_path / "exports", root=release_root)
    module = _website_adapter()
    bundle = read_json(exported)
    data = module.build_data(bundle, load_release(root=release_root), {"models": {"model-a": {"label": "Model A", "provider": "OpenAI"}}}, "commit-sha")
    result = data["results"]
    assert result["benchmark"]["version"] == "v1"
    assert result["task_count"] == 2
    assert result["models"][0]["label"] == "Model A"
    assert result["models"][0]["overall"]["passed"] == 4
    assert result["models"][0]["overall"]["rate"] == 1
    assert result["models"][0]["overall"]["ci"] == [1, 1]
    assert result["models"][0]["overall"]["median_seconds"] is None
    target = module.write_data(data, tmp_path / "display")
    original = (target / "task-results.csv").read_bytes()
    assert module.write_data(data, tmp_path / "display") == target
    assert (target / "task-results.csv").read_bytes() == original
    data["answers"][0]["passed"] = False
    with pytest.raises(ValueError, match="immutable"):
        module.write_data(data, tmp_path / "display")
    assert (target / "task-results.csv").read_bytes() == original


@pytest.mark.parametrize("change", ["coverage", "category", "version"])
def test_website_refuses_incompatible_display_inputs(release_root: Path, tmp_path: Path, change: str) -> None:
    exported = export_results(inputs(release_root, tmp_path), tmp_path / "exports", root=release_root)
    bundle = read_json(exported)
    if change == "coverage":
        bundle["models"][0]["answers"].pop()
    elif change == "category":
        bundle["models"][0]["answers"][0]["category"] = "another-category"
    elif change == "version":
        bundle["benchmark"]["version"] = "v2"
    bundle["fingerprint"] = digest({key: value for key, value in bundle.items()
                                     if key not in {"snapshot_id", "fingerprint", "updated_at"}})
    with pytest.raises(ValueError):
        _website_adapter().build_data(bundle, load_release(root=release_root), {}, "commit-sha")


def test_pinned_baseline_release_rejects_skills_ab_before_generation(release_root: Path) -> None:
    cfg = config(pinned=True)
    cfg.skills.paths = [Path('some-skill.md')]
    tests = _parse_execution_suite(release_root / 'datasets/suites/wp-core/execution/tasks.json')
    with pytest.raises(ValueError, match='Skills A/B runs are diagnostic'):
        capture_identity(cfg, tests, root=release_root)
