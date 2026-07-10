"""Tests for config-means-behavior: no silent no-op fields."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wp_bench.config import (
    GraderConfig,
    HarnessConfig,
    OutputConfig,
    RunConfig,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_http_grader_rejected_until_implemented() -> None:
    with pytest.raises(ValidationError, match="not supported yet"):
        GraderConfig(kind="http")  # type: ignore[arg-type]


def test_unknown_grader_field_rejected() -> None:
    """Removed fields (url, concurrency) fail loudly instead of no-oping."""
    with pytest.raises(ValidationError):
        GraderConfig(url="http://example.com")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        GraderConfig(concurrency=4)  # type: ignore[call-arg]


def test_removed_output_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        OutputConfig(save_prompts=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OutputConfig(save_artifacts_dir="artifacts")  # type: ignore[call-arg]


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        HarnessConfig(unknown_section={"a": 1})  # type: ignore[call-arg]


def test_skip_both_dimensions_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot both be true"):
        RunConfig(skip_runtime=True, skip_static=True)


def test_skip_runtime_alone_is_valid() -> None:
    config = RunConfig(skip_runtime=True)
    assert config.skip_runtime is True
    assert config.skip_static is False


def test_example_config_loads() -> None:
    """The shipped example must only use supported fields."""
    example = PROJECT_ROOT / "wp-bench.example.yaml"
    config = HarnessConfig.from_file(example)
    assert config.run.suite == "wp-core-v1"
    assert config.grader.kind in ("docker", "cli")


def test_skip_runtime_controls_verification_payload() -> None:
    """skip_runtime must actually strip runtime checks from the verifier spec."""
    from wp_bench.core import _build_verification_spec
    from wp_bench.datasets import ExecutionTest

    test = ExecutionTest(
        id="e-one",
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        test_type="execution",
        category="general",
        difficulty="basic",
        requirements=[],
        test_function=None,
        static_checks={"required_patterns": [{"pattern": "x", "weight": 1}]},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution=None,
        metadata={},
    )

    default_spec = _build_verification_spec(test, HarnessConfig())
    assert default_spec["static_checks"] == test.static_checks
    assert default_spec["runtime_checks"] == test.runtime_checks

    skip_runtime = HarnessConfig(run=RunConfig(skip_runtime=True))
    spec = _build_verification_spec(test, skip_runtime)
    assert spec["runtime_checks"] == {}
    assert spec["static_checks"] == test.static_checks

    skip_static = HarnessConfig(run=RunConfig(skip_static=True))
    spec = _build_verification_spec(test, skip_static)
    assert spec["static_checks"] == {}
    assert spec["runtime_checks"] == test.runtime_checks


def test_skipped_dimension_not_scored() -> None:
    """A skipped dimension is not applicable: it cannot zero the score."""
    from wp_bench.core import BenchmarkRunner
    from wp_bench.datasets import ExecutionTest

    test = ExecutionTest(
        id="e-one",
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        test_type="execution",
        category="general",
        difficulty="basic",
        requirements=[],
        test_function=None,
        static_checks={"required_patterns": [{"pattern": "x", "weight": 1}]},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution=None,
        metadata={},
    )
    # Runtime skipped: raw has no runtime result, static passed fully.
    raw = {"success": True, "static": {"score": 1.0, "details": {"total_weight": 1}}}

    score = BenchmarkRunner._score_correctness(raw, test, skip_runtime=True)

    # Without the skip flag this would crash-detect (no runtime weight) -> 0.0.
    assert score == 1.0
