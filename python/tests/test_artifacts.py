"""Tests for artifact parsing, validation, and execution routing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import fake_generation

from wp_bench.artifacts import (
    MAX_ARTIFACT_FILES,
    Artifact,
    ArtifactError,
    parse_artifact,
    render_artifact_instructions,
)
from wp_bench.config import (
    DatasetConfig,
    GraderConfig,
    HarnessConfig,
    ModelConfig,
    OutputConfig,
    RunConfig,
)
from wp_bench.core import BenchmarkRunner
from wp_bench.datasets import ExecutionTest
from wp_bench.environment import ExecutionResult

PLUGIN_JSON = json.dumps(
    {
        "files": {
            "demo-plugin.php": "<?php\n/*\nPlugin Name: Demo\n*/\nadd_action('init', 'demo_init');",
            "includes/helpers.php": "<?php\nfunction demo_helper() { return true; }",
        }
    }
)


def _plugin_test(test_id: str = "e-plugin-001") -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Build a plugin.",
        expected_behavior="expected",
        category="plugins",
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={"assertions": [{"type": "function_exists", "target": "demo_helper", "weight": 1}]},
        reference_solution=None,
        metadata={},
        artifact_kind="wp_plugin_files",
        reference_files={"demo-plugin.php": "<?php\n/*\nPlugin Name: Demo\n*/"},
    )


# --- Parsing ---------------------------------------------------------------


def test_php_snippet_artifact_still_supported() -> None:
    artifact = parse_artifact("```php\nfunction x() {}\n```", "php_snippet")

    assert artifact.kind == "php_snippet"
    assert artifact.code == "function x() {}"
    assert artifact.files == {}
    assert artifact.payload_fields() == {"artifact_kind": "php_snippet", "code": "function x() {}"}


def test_parse_plugin_files_json_output() -> None:
    artifact = parse_artifact(PLUGIN_JSON, "wp_plugin_files")

    assert artifact.kind == "wp_plugin_files"
    assert set(artifact.files) == {"demo-plugin.php", "includes/helpers.php"}
    assert artifact.payload_fields()["files"] == artifact.files


def test_parse_plugin_files_accepts_fenced_json() -> None:
    artifact = parse_artifact(f"```json\n{PLUGIN_JSON}\n```", "wp_plugin_files")

    assert "demo-plugin.php" in artifact.files


def test_plugin_artifact_rejects_invalid_json() -> None:
    with pytest.raises(ArtifactError, match="JSON"):
        parse_artifact("here is your plugin: <?php ...", "wp_plugin_files")


def test_plugin_artifact_rejects_path_traversal() -> None:
    payload = json.dumps({"files": {"../evil.php": "<?php /* Plugin Name: X */"}})
    with pytest.raises(ArtifactError, match="traversal"):
        parse_artifact(payload, "wp_plugin_files")


def test_plugin_artifact_rejects_absolute_paths() -> None:
    payload = json.dumps({"files": {"/etc/evil.php": "<?php /* Plugin Name: X */"}})
    with pytest.raises(ArtifactError, match="relative"):
        parse_artifact(payload, "wp_plugin_files")


def test_plugin_artifact_rejects_backslash_traversal() -> None:
    payload = json.dumps({"files": {"..\\evil.php": "<?php /* Plugin Name: X */"}})
    with pytest.raises(ArtifactError, match="traversal"):
        parse_artifact(payload, "wp_plugin_files")


def test_plugin_artifact_requires_main_plugin_file() -> None:
    payload = json.dumps({"files": {"helpers.php": "<?php function x() {}"}})
    with pytest.raises(ArtifactError, match="Plugin Name"):
        parse_artifact(payload, "wp_plugin_files")


def test_plugin_artifact_rejects_oversized_file() -> None:
    payload = json.dumps(
        {"files": {"demo.php": "<?php /* Plugin Name: X */" + "a" * 300_000}}
    )
    with pytest.raises(ArtifactError, match="too large"):
        parse_artifact(payload, "wp_plugin_files")


def test_plugin_artifact_rejects_too_many_files() -> None:
    files = {f"f{i}.php": "<?php" for i in range(MAX_ARTIFACT_FILES + 1)}
    files["demo.php"] = "<?php /* Plugin Name: X */"
    with pytest.raises(ArtifactError, match="too many"):
        parse_artifact(json.dumps({"files": files}), "wp_plugin_files")


def test_unknown_artifact_kind_rejected() -> None:
    with pytest.raises(ArtifactError, match="Unsupported artifact kind"):
        parse_artifact("anything", "js_module")


def test_prompt_instructions_differ_by_kind() -> None:
    snippet = render_artifact_instructions("php_snippet")
    plugin = render_artifact_instructions("wp_plugin_files")

    assert "```php" in snippet
    assert "files" in plugin
    assert snippet != plugin


# --- Runner integration ----------------------------------------------------


def _config(tmp_path: Path, **run_overrides: object) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig.model_validate(dict(run_overrides)),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )


def _passing_result() -> ExecutionResult:
    raw = {
        "success": True,
        "static": {"score": 1.0, "details": {}},
        "runtime": {"score": 1.0, "details": {"total_weight": 1}},
    }
    return ExecutionResult(success=True, raw=raw, stdout="", stderr="")


def test_plugin_artifact_payload_reaches_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A plugin completion is parsed and shipped as files, not code."""
    test = _plugin_test()
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [test],
    )
    runner = BenchmarkRunner(_config(tmp_path))
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    seen: dict = {}

    def fake_execute_artifact(artifact: Artifact, verification_spec: dict) -> ExecutionResult:
        seen["artifact"] = artifact
        seen["spec"] = verification_spec
        return _passing_result()

    runner.environment.execute_artifact = fake_execute_artifact  # type: ignore[method-assign]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation(PLUGIN_JSON)
    )

    payload = runner.run()

    assert seen["artifact"].kind == "wp_plugin_files"
    assert "demo-plugin.php" in seen["artifact"].files
    record = payload["results"][0]
    assert record["scores"]["execution_pass"] is True


def test_artifact_parse_failure_is_scored_failure_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unparseable completions fail the test and the run continues."""
    tests = [_plugin_test("e-plugin-001"), _plugin_test("e-plugin-002")]
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: tests,
    )
    runner = BenchmarkRunner(_config(tmp_path))
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    runner.environment.execute_artifact = lambda artifact, verification_spec: _passing_result()  # type: ignore[method-assign]
    completions = iter(["not json at all", PLUGIN_JSON])
    monkeypatch.setattr(
        runner.model,
        "generate_with_metadata",
        lambda prompt: fake_generation(next(completions)),
    )

    payload = runner.run()

    records = {record["test_id"]: record for record in payload["results"]}
    failed = records["e-plugin-001"]
    passed = records["e-plugin-002"]
    assert failed["scores"]["execution_pass"] is False
    assert failed["scores"]["correctness"] == 0.0
    assert "artifact_error" in failed["grader"]["raw"]
    assert passed["scores"]["execution_pass"] is True


def test_reference_solution_uses_reference_files_for_plugin_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test = _plugin_test()
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [test],
    )
    runner = BenchmarkRunner(_config(tmp_path, check_reference_solution=True))
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    seen: dict = {}

    def fake_execute_artifact(artifact: Artifact, verification_spec: dict) -> ExecutionResult:
        seen["artifact"] = artifact
        return _passing_result()

    runner.environment.execute_artifact = fake_execute_artifact  # type: ignore[method-assign]

    runner.run()

    assert seen["artifact"].kind == "wp_plugin_files"
    assert seen["artifact"].files == test.reference_files


def test_snippet_prompt_unchanged_plugin_prompt_asks_for_json(
    tmp_path: Path,
) -> None:
    snippet_test = _plugin_test()
    snippet_test.artifact_kind = "php_snippet"
    plugin_test = _plugin_test()

    snippet_prompt = BenchmarkRunner._render_execution_prompt(snippet_test)
    plugin_prompt = BenchmarkRunner._render_execution_prompt(plugin_test)

    assert "```php" in snippet_prompt
    assert "files" in plugin_prompt
