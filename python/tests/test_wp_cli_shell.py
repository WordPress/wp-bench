"""WP-CLI artifact transport, suite selection, and dataset contracts."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from conftest import fake_generation
from typer.testing import CliRunner

from wp_bench.artifacts import MAX_FILE_BYTES, Artifact, ArtifactError, parse_artifact
from wp_bench.cli import app
from wp_bench.config import DatasetConfig, HarnessConfig, OutputConfig, RunConfig
from wp_bench.core import BenchmarkRunner
from wp_bench.datasets import load_tests
from wp_bench.environment import ExecutionResult
from wp_bench.exploits import exploit_candidates


@pytest.mark.parametrize("fenced", [False, True])
def test_shell_artifact_preserves_script_as_a_string(fenced: bool) -> None:
    script = "ids=$(wp post list --post_status=draft --format=ids)\n" \
        "for id in $ids; do wp post meta update \"$id\" note 'a \"quote\" & spaces'; done"
    completion = f"```bash\n{script}\n```" if fenced else script
    artifact = parse_artifact(completion, "wp_cli_shell")
    assert artifact.payload_fields() == {"artifact_kind": "wp_cli_shell", "code": script}


@pytest.mark.parametrize("script", ["", "  \n", "wp\x00post", "é" * MAX_FILE_BYTES])
def test_shell_artifact_rejects_invalid_size_or_content(script: str) -> None:
    with pytest.raises(ArtifactError, match="nonempty, NUL-free"):
        parse_artifact(script, "wp_cli_shell")


@pytest.mark.parametrize("mode", ["model", "reference", "exploits"])
def test_shell_scripts_reach_the_runtime_in_every_mode(monkeypatch, tmp_path, mode) -> None:
    test = load_tests(DatasetConfig(source="local", name="wp-cli-v1"))[0]
    reference = test.reference_solution
    assert reference is not None
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [test])
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-cli-v1"),
        run=RunConfig(
            suite="wp-cli-v1", check_reference_solution=mode == "reference",
            check_exploits=mode == "exploits",
        ),
        output=OutputConfig(path=tmp_path / "result.json", jsonl_path=None),
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda **kwargs: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    artifacts: list[Artifact] = []

    def execute(artifact: Artifact, spec: dict) -> ExecutionResult:
        assert spec["timeout_seconds"] == config.grader.timeout_seconds
        assert spec["runtime_checks"] == test.runtime_checks
        artifacts.append(artifact)
        raw = {"runtime": {"score": 0 if mode == "exploits" else 1,
                           "details": {"total_weight": 1}}}
        return ExecutionResult(success=True, raw=raw, stdout="", stderr="")

    monkeypatch.setattr(runner.environment, "execute_artifact", execute)
    monkeypatch.setattr(runner.model, "generate_with_metadata",
                        lambda prompt: fake_generation(reference))
    runner.run()
    expected = [code for _, code in exploit_candidates(test)] if mode == "exploits" else [reference]
    assert [artifact.code for artifact in artifacts] == expected
    assert all(artifact.kind == "wp_cli_shell" and not artifact.files for artifact in artifacts)


def test_shell_prompt_describes_the_actual_execution_contract() -> None:
    test = load_tests(DatasetConfig(source="local", name="wp-cli-v1"))[0]
    prompt = BenchmarkRunner._render_execution_prompt(test)
    assert "Bash command or script" in prompt
    assert "SQLite" in prompt and "no network" in prompt
    assert "Each wp invocation boots WordPress separately" in prompt
    assert "Do not return JSON, an array" in prompt
    assert "Return only valid PHP" not in prompt
    assert "init action has already fired" not in prompt


def test_cli_suite_flag_selects_the_local_cli_suite(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("dataset: {source: local, name: wp-core-v1}\n")
    result = CliRunner().invoke(app, ["run", "--config", str(path), "--suite", "wp-cli-v1", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Execution tests: 24" in result.output


def test_huggingface_suite_filter_preserves_repository_name(monkeypatch, tmp_path) -> None:
    seen = []

    def load(name, **kwargs):
        seen.append(name)
        return [
            {"id": suite, "suite": suite, "test_kind": "execution", "prompt": "Task",
             "artifact_kind": kind}
            for suite, kind in [("wp-core-v1", "php_snippet"), ("wp-cli-v1", "wp_cli_shell")]
        ]

    monkeypatch.setattr("wp_bench.datasets.hf_load_dataset", load)
    selected = load_tests(DatasetConfig(source="huggingface", name="WordPress/wp-bench-v1", suite="wp-cli-v1"))
    assert [t.artifact_kind for t in selected] == ["wp_cli_shell"]
    path = tmp_path / "config.yaml"
    path.write_text("dataset: {source: huggingface, name: WordPress/wp-bench-v1}\n")
    result = CliRunner().invoke(app, ["run", "--config", str(path), "--suite", "wp-cli-v1", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Execution tests: 1" in result.output
    assert seen == ["WordPress/wp-bench-v1"] * 2


def test_cli_suite_has_balanced_workflows_with_behavioral_and_exploit_checks() -> None:
    tests = load_tests(DatasetConfig(source="local", name="wp-cli-v1"))
    assert Counter(t.category for t in tests) == {
        "cli-content": 4, "cli-taxonomy-meta": 4, "cli-users": 4,
        "cli-options": 4, "cli-reporting": 4, "cli-maintenance": 4,
    }
    core = load_tests(DatasetConfig(source="local", name="wp-core-v1"))
    assert len(core) == 350
    assert len({t.id for t in core + tests}) == 374
    for test in tests:
        assert test.suite == "wp-cli-v1" and test.artifact_kind == "wp_cli_shell"
        assert test.test_function is None
        assert test.reference_solution and "wp " in test.reference_solution
        assert test.exploit_solutions and all(s.strip() for s in test.exploit_solutions)
        assert test.prompt != test.expected_behavior
        assert test.metadata["source_refs"]
        assert test.runtime_checks["setup"]
        assert test.runtime_checks["assertions"]
        assert all(a["type"] == "custom_assertion" for a in test.runtime_checks["assertions"])
        assert len(exploit_candidates(test)) >= 4
    assert sum(t.runtime_checks.get("repeat", 1) > 1 for t in tests) >= 6


def test_cli_export_preserves_shell_contract_and_excludes_exploits() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "datasets" / "export_dataset.py"
    spec = importlib.util.spec_from_file_location("export_dataset", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = module.load_suite("wp-cli-v1")
    assert len(rows) == 24
    assert all(r["artifact_kind"] == "wp_cli_shell" and "exploit_solutions" not in r for r in rows)
    local = {t.id: t for t in load_tests(DatasetConfig(source="local", name="wp-cli-v1"))}
    for row in rows:
        assert row["reference_solution"] == local[row["id"]].reference_solution
        assert json.loads(row["runtime_checks"]) == local[row["id"]].runtime_checks
