"""Integration tests for skill-injection A/B variant runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import fake_generation
from typer.testing import CliRunner

from wp_bench.cli import app
from wp_bench.config import (
    DatasetConfig,
    GraderConfig,
    HarnessConfig,
    ModelConfig,
    OutputConfig,
    RunConfig,
    SkillsConfig,
)
from wp_bench.core import MultiModelRunner
from wp_bench.datasets import ExecutionTest
from wp_bench.environment import ExecutionResult
from wp_bench.output import print_comparison_table, print_skill_impact
from wp_bench.skills import build_variants, load_skill


def _execution_test(test_id: str = "e-one") -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="hooks",
        difficulty="basic",
        requirements=["Requirement"],
        test_function=None,
        static_checks={"required_patterns": [{"pattern": "code", "weight": 1}]},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution="function ref() { return true; }",
        metadata={},
    )


def _passing_result() -> ExecutionResult:
    raw = {
        "success": True,
        "static": {"score": 1.0, "details": {"total_weight": 1}},
        "runtime": {"score": 1.0, "details": {"total_weight": 1}},
    }
    return ExecutionResult(success=True, raw=raw, stdout="out", stderr="")


class FakeEnvironment:
    def setup(self, *, capture_baseline: bool = True) -> None: ...
    def reset(self) -> None: ...

    def execute_artifact(self, artifact: object, spec: dict) -> ExecutionResult:
        return _passing_result()


def _skill_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "wp-test-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: wp-test-skill\ndescription: Test skill.\n---\n\nGuidance body.",
        encoding="utf-8",
    )
    return skill


def _config(tmp_path: Path, skill_path: Path) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        models=[ModelConfig(name="model-a")],
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
        skills=SkillsConfig(paths=[skill_path]),
    )


def _run_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[MultiModelRunner, list[str | None]]:
    """Run a 1-model skills A/B matrix; returns runner + captured system prompts."""
    skill = load_skill(_skill_dir(tmp_path))
    config = _config(tmp_path, Path(skill.source_path))

    system_prompts: list[str | None] = []

    class FakeModel:
        def __init__(self, model_config: ModelConfig, system_prompt: str | None = None):
            system_prompts.append(system_prompt)

        def generate_with_metadata(self, prompt: str) -> Any:
            return fake_generation("```php\ncode\n```")

    monkeypatch.setattr("wp_bench.core.ModelInterface", FakeModel)
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [_execution_test()])

    runner = MultiModelRunner(config, skills=[skill])
    runner.environment = FakeEnvironment()  # type: ignore[assignment]
    runner.run()
    return runner, system_prompts


def test_matrix_runs_baseline_and_skills_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, system_prompts = _run_matrix(monkeypatch, tmp_path)

    assert list(runner.results.keys()) == ["model-a", "model-a+skills"]
    # Baseline pass has no system prompt; skills pass injects the skill.
    assert system_prompts[0] is None
    assert system_prompts[1] is not None
    assert "# Skill: wp-test-skill" in system_prompts[1]

    baseline = runner.results["model-a"]
    skilled = runner.results["model-a+skills"]
    assert baseline["base_model"] == skilled["base_model"] == "model-a"
    assert baseline["variant"]["key"] == "baseline"
    assert skilled["variant"]["key"] == "skills"
    assert skilled["variant"]["skills"] == ["wp-test-skill"]

    # Same test, same user prompt: prompt_hash identical across variants.
    base_record = baseline["results"][0]
    skill_record = skilled["results"][0]
    assert base_record["prompt_hash"] == skill_record["prompt_hash"]
    assert base_record["variant"]["key"] == "baseline"
    assert skill_record["variant"]["key"] == "skills"
    assert skill_record["variant"]["system_prompt_hash"]
    assert base_record["variant"].keys() == skill_record["variant"].keys()


def test_matrix_payload_contains_skills_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _run_matrix(monkeypatch, tmp_path)

    payload_path = max(tmp_path.glob("results_*.json"))
    payload = json.loads(payload_path.read_text())

    metadata = payload["metadata"]
    assert metadata["variants"] == ["baseline", "skills"]
    assert metadata["skills"]["enabled"] is True
    assert metadata["skills"]["include_references"] is True
    assert metadata["skills"]["system_prompt_sha256"]
    (provenance,) = metadata["skills"]["skills"]
    assert provenance["name"] == "wp-test-skill"
    assert provenance["content_sha256"]

    assert set(payload["models"]) == {"model-a", "model-a+skills"}
    entry = payload["models"]["model-a+skills"]
    assert entry["base_model"] == "model-a"
    assert entry["variant"]["kind"] == "inject"


def test_skills_only_skips_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    skill = load_skill(_skill_dir(tmp_path))
    config = _config(tmp_path, Path(skill.source_path))
    config.skills.only = True

    monkeypatch.setattr(
        "wp_bench.core.ModelInterface",
        lambda model_config, system_prompt=None: type(
            "FakeModel",
            (),
            {"generate_with_metadata": staticmethod(lambda prompt: fake_generation("```php\ncode\n```"))},
        )(),
    )
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [_execution_test()])

    runner = MultiModelRunner(config, skills=[skill])
    runner.environment = FakeEnvironment()  # type: ignore[assignment]
    runner.run()

    assert list(runner.results.keys()) == ["model-a+skills"]


def test_duplicate_display_names_rejected(tmp_path: Path) -> None:
    skill = load_skill(_skill_dir(tmp_path))
    config = _config(tmp_path, Path(skill.source_path))
    config.models = [ModelConfig(name="model-a"), ModelConfig(name="model-a")]

    runner = MultiModelRunner(config, skills=[skill])
    with pytest.raises(ValueError, match="Duplicate result keys"):
        runner._ensure_unique_display_names(config.get_models())


def test_comparison_table_renders_delta_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def _entry(variant_key: str, pass_rate: float, cost: float) -> dict[str, Any]:
        return {
            "base_model": "model-a",
            "variant": {"key": variant_key},
            "scores": {
                "execution_pass_rate": pass_rate,
                "runtime": pass_rate,
                "overall": pass_rate,
            },
            "usage": {"estimated_cost_usd": cost, "median_latency_ms": 100.0},
        }

    from rich.console import Console

    recording_console = Console(record=True, width=200)
    monkeypatch.setattr("wp_bench.output.console", recording_console)
    print_comparison_table(
        {
            "model-a": _entry("baseline", 0.5, 0.01),
            "model-a+skills": _entry("skills", 0.75, 0.03),
        }
    )
    output = recording_console.export_text()
    assert "model-a+skills" in output
    assert "Δ skills" in output
    assert "+25.0pp" in output
    assert "single run per variant" in output


def test_comparison_table_without_variants_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_comparison_table(
        {
            "model-a": {
                "scores": {"execution_pass_rate": 0.5, "runtime": 0.5, "overall": 0.5},
                "usage": {},
            }
        }
    )
    output = capsys.readouterr().out
    assert "model-a" in output
    assert "Δ" not in output


def _impact_record(
    test_id: str, *, execution_pass: bool, runtime: float | None = None, error: bool = False
) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "scores": {"execution_pass": execution_pass, "runtime": runtime},
        "error": {"type": "Timeout", "message": "boom"} if error else None,
    }


def _render_skill_impact(
    monkeypatch: pytest.MonkeyPatch, results: dict[str, dict[str, Any]]
) -> str:
    from rich.console import Console

    recording_console = Console(record=True, width=200)
    monkeypatch.setattr("wp_bench.output.console", recording_console)
    print_skill_impact(results)
    return recording_console.export_text()


def test_skill_impact_lists_flipped_and_still_failing_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = {
        "model-a": {
            "base_model": "model-a",
            "variant": {"key": "baseline"},
            "scores": {},
            "results": [
                _impact_record("e-fixed", execution_pass=False, runtime=0.5),
                _impact_record("e-broken", execution_pass=True, runtime=1.0),
                _impact_record("e-stuck", execution_pass=False, runtime=0.0),
                _impact_record("e-fine", execution_pass=True, runtime=1.0),
                _impact_record("e-partial", execution_pass=False, runtime=0.25),
            ],
        },
        "model-a+skills": {
            "base_model": "model-a",
            "variant": {"key": "skills"},
            "scores": {},
            "results": [
                _impact_record("e-fixed", execution_pass=True, runtime=1.0),
                _impact_record("e-broken", execution_pass=False, runtime=0.5),
                _impact_record("e-stuck", execution_pass=False, runtime=0.0),
                _impact_record("e-fine", execution_pass=True, runtime=1.0),
                _impact_record("e-partial", execution_pass=False, runtime=0.75),
            ],
        },
    }
    output = _render_skill_impact(monkeypatch, results)

    assert "Skill Impact per Test (model-a)" in output
    assert "e-fixed" in output and "fixed by skill" in output
    assert "e-broken" in output and "broken by skill" in output
    # Runtime movement on a still-failing test is surfaced too.
    assert "e-partial" in output and "runtime 25% → 75%" in output
    # Tests failing in both variants are rows, marked as still failing.
    assert "e-stuck" in output
    assert "still failing" in output
    # Tests passing in both variants are only counted, never listed.
    assert "Passing in both variants (not shown): 1" in output
    assert "e-fine" not in output


def test_skill_impact_silent_without_variant_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _render_skill_impact(
        monkeypatch,
        {"model-a": {"scores": {}, "results": [_impact_record("e-1", execution_pass=True)]}},
    )
    assert output.strip() == ""


def test_skill_impact_handles_errored_records(monkeypatch: pytest.MonkeyPatch) -> None:
    results = {
        "model-a": {
            "base_model": "model-a",
            "variant": {"key": "baseline"},
            "scores": {},
            "results": [_impact_record("e-err", execution_pass=False, error=True)],
        },
        "model-a+skills": {
            "base_model": "model-a",
            "variant": {"key": "skills"},
            "scores": {},
            "results": [_impact_record("e-err", execution_pass=True, runtime=1.0)],
        },
    }
    output = _render_skill_impact(monkeypatch, results)
    assert "e-err" in output
    assert "error" in output
    assert "fixed by skill" in output


def test_build_variants_shares_selection_config(tmp_path: Path) -> None:
    """Both variants come from one config: selection (seed/limit) is identical."""
    skill = load_skill(_skill_dir(tmp_path))
    variants = build_variants([skill])
    assert [variant.key for variant in variants] == ["baseline", "skills"]
    # Variants carry no test-selection state at all — only injection state.
    assert {field for field in variants[0].__dataclass_fields__} == {
        "key",
        "kind",
        "label_suffix",
        "skills",
        "system_prompt",
    }


def test_cli_routes_single_model_with_skill_to_multi_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill_dir = _skill_dir(tmp_path)
    captured: dict[str, Any] = {}

    class FakeMultiRunner:
        def __init__(self, config: HarnessConfig, skills: list[Any] | None = None):
            captured["config"] = config
            captured["skills"] = skills

        def run(self) -> dict[str, Any]:
            return {}

    monkeypatch.setattr("wp_bench.cli.MultiModelRunner", FakeMultiRunner)

    result = CliRunner().invoke(
        app, ["run", "--model-name", "model-a", "--skill", str(skill_dir)]
    )
    assert result.exit_code == 0, result.output
    assert captured["config"].models == [ModelConfig(name="model-a")]
    assert captured["config"].model is None
    assert [loaded.name for loaded in captured["skills"]] == ["wp-test-skill"]


def test_cli_rejects_skills_with_audit_modes(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    result = CliRunner().invoke(
        app, ["run", "--skill", str(skill_dir), "--check-exploits"]
    )
    assert result.exit_code == 1
    assert "audit modes" in result.output


def test_cli_fails_fast_on_bad_skill_path() -> None:
    result = CliRunner().invoke(app, ["run", "--skill", "/nonexistent/skill"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
