"""Tests for config field validation (Pydantic v2 style)."""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from wp_bench.config import (
    GraderConfig,
    HarnessConfig,
    ModelConfig,
    RunConfig,
    SkillsConfig,
)


def test_model_config_rejects_temperature_below_zero() -> None:
    with pytest.raises(ValidationError, match="between 0 and 2"):
        ModelConfig(temperature=-0.1)


def test_model_config_rejects_temperature_above_two() -> None:
    with pytest.raises(ValidationError, match="between 0 and 2"):
        ModelConfig(temperature=2.1)


def test_model_config_accepts_temperature_bounds() -> None:
    assert ModelConfig(temperature=0.0).temperature == 0.0
    assert ModelConfig(temperature=2.0).temperature == 2.0


def test_model_config_rejects_invalid_top_p() -> None:
    with pytest.raises(ValidationError, match="between 0 and 1"):
        ModelConfig(top_p=1.5)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        ModelConfig(top_p=-0.1)


def test_model_config_accepts_valid_top_p() -> None:
    assert ModelConfig(top_p=0.9).top_p == 0.9
    assert ModelConfig(top_p=None).top_p is None


def test_run_config_rejects_removed_fields() -> None:
    """Removed fields (test_type, concurrency) fail loudly instead of no-oping."""
    with pytest.raises(ValidationError):
        RunConfig(test_type="execution")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RunConfig(concurrency=5)  # type: ignore[call-arg]


def test_run_config_rejects_zero_limit() -> None:
    with pytest.raises(ValidationError):
        RunConfig(limit=0)


def test_grader_config_rejects_nonpositive_timeouts() -> None:
    with pytest.raises(ValidationError):
        GraderConfig(timeout_seconds=0)
    with pytest.raises(ValidationError):
        GraderConfig(setup_timeout_seconds=-1)


def test_skills_config_defaults() -> None:
    skills = HarnessConfig().skills
    assert skills.paths == []
    assert skills.include_references is True
    assert skills.only is False


def test_skills_config_rejects_only_without_paths() -> None:
    with pytest.raises(ValidationError, match="skills.only requires skills.paths"):
        SkillsConfig(only=True)


def test_skills_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SkillsConfig(mode="agentic")  # type: ignore[call-arg]


def test_from_file_resolves_relative_skill_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "wp-bench.yaml"
    config_path.write_text(
        "skills:\n  paths:\n    - skills/example\n    - /abs/skill\n",
        encoding="utf-8",
    )
    config = HarnessConfig.from_file(config_path)
    assert config.skills.paths == [
        (tmp_path / "skills/example").resolve(),
        Path("/abs/skill"),
    ]


def test_config_construction_emits_no_deprecation_warnings() -> None:
    """The config layer is fully on Pydantic v2 idioms."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        HarnessConfig()
        ModelConfig(temperature=1.0, top_p=0.5)
        RunConfig(limit=5)
        GraderConfig(kind="cli")
