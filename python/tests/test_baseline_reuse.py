"""Tests for reusing a stored baseline pass across skill-authoring iterations.

The baseline arm cannot change while a skill is edited, so re-grading it every
round is pure cost. Reuse is only sound when the stored pass measured the same
thing, so most of what matters here is what gets rejected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import pytest

from wp_bench.skills import BaselineReuseError, load_baseline

SCORING = "3.0"
SCHEMA = "2.1"


def _payload(
    *,
    models: dict[str, list[str]],
    scoring: str = SCORING,
    schema: str = SCHEMA,
    variant_key: str = "baseline",
) -> dict[str, Any]:
    """A previous run's payload with one baseline entry per model."""
    return {
        "metadata": {"scoring_version": scoring, "result_schema_version": schema},
        "models": {
            name: {
                "config": {"name": name},
                "base_model": name,
                "variant": {"key": variant_key, "kind": "none"},
                "scores": {"overall": 0.5},
                "usage": {"estimated_cost_usd": 0.01},
                "results": [{"test_id": test_id} for test_id in test_ids],
            }
            for name, test_ids in models.items()
        },
    }


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "previous.json"
    path.write_bytes(orjson.dumps(payload))
    return path


def _load(path: Path, **overrides: Any):
    kwargs: dict[str, Any] = {
        "model_names": ["model-a"],
        "test_ids": {"e-one", "e-two"},
        "scoring_version": SCORING,
        "schema_version": SCHEMA,
    }
    kwargs.update(overrides)
    return load_baseline(path, **kwargs)


def test_reuses_a_matching_baseline(tmp_path: Path) -> None:
    path = _write(tmp_path, _payload(models={"model-a": ["e-one", "e-two"]}))

    baseline = _load(path)

    assert set(baseline.entries) == {"model-a"}
    assert baseline.entries["model-a"]["scores"]["overall"] == 0.5
    # Provenance travels with it, so results never hide the recycling.
    assert baseline.provenance()["source_path"] == str(path.resolve())
    assert baseline.provenance()["models"] == ["model-a"]


def test_rejects_a_baseline_that_graded_other_tests(tmp_path: Path) -> None:
    """The delta would otherwise compare two different measurements."""
    path = _write(tmp_path, _payload(models={"model-a": ["e-one", "e-three"]}))

    with pytest.raises(BaselineReuseError, match="different test set"):
        _load(path)


def test_rejects_a_baseline_missing_a_model_in_this_run(tmp_path: Path) -> None:
    path = _write(tmp_path, _payload(models={"model-a": ["e-one", "e-two"]}))

    with pytest.raises(BaselineReuseError, match="no baseline pass"):
        _load(path, model_names=["model-a", "model-b"])


def test_rejects_a_baseline_from_another_scoring_version(tmp_path: Path) -> None:
    """Scores across a scoring bump are not comparable by construction."""
    path = _write(tmp_path, _payload(models={"model-a": ["e-one", "e-two"]}, scoring="2.0"))

    with pytest.raises(BaselineReuseError, match="scoring version"):
        _load(path)


def test_rejects_a_baseline_from_another_schema_version(tmp_path: Path) -> None:
    path = _write(tmp_path, _payload(models={"model-a": ["e-one", "e-two"]}, schema="1.0"))

    with pytest.raises(BaselineReuseError, match="result schema version"):
        _load(path)


def test_ignores_skills_passes_when_indexing_baselines(tmp_path: Path) -> None:
    """A results file holds both arms; only the baseline arm is reusable."""
    path = _write(tmp_path, _payload(models={"model-a": ["e-one", "e-two"]}, variant_key="skills"))

    with pytest.raises(BaselineReuseError, match="no baseline pass"):
        _load(path)


def test_rejects_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "previous.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(BaselineReuseError, match="Cannot read baseline results"):
        _load(path)


def test_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BaselineReuseError, match="Cannot read baseline results"):
        _load(tmp_path / "nope.json")
