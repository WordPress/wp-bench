"""Shape checks for the per-model leaderboard files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

RESULTS_DIR = Path(__file__).resolve().parents[2] / "leaderboard" / "results"
SCORE_KEYS = {"correctness", "execution_pass_rate", "runtime", "overall"}
SUBMISSION_KEYS = {
    "submitted_by",
    "source",
    "run_date",
    "reported_at",
    "tests",
    "notes",
}


def result_files() -> list[Path]:
    return sorted(RESULTS_DIR.glob("*.json"))


def test_results_folder_is_not_empty() -> None:
    assert result_files(), f"no leaderboard files in {RESULTS_DIR}"


@pytest.mark.parametrize("path", result_files(), ids=lambda p: p.name)
def test_result_file_shape(path: Path) -> None:
    document = json.loads(path.read_text())
    assert set(document) == {"metadata", "submission"}, (
        f"unexpected top-level keys in {path.name}"
    )

    metadata = document["metadata"]
    model = metadata["model"]["name"]
    assert model and isinstance(model, str)
    assert path.name == model.replace("/", "__") + ".json", (
        f"{path.name} must be named after its model id ({model})"
    )
    assert SCORE_KEYS <= set(metadata["scores"]), f"{path.name} is missing score keys"
    for key, value in metadata["scores"].items():
        if value is not None:
            assert 0.0 <= value <= 1.0, (
                f"{path.name} scores.{key}={value} is not a 0-1 fraction"
            )
    assert metadata["scores"]["overall"] is not None, (
        f"{path.name} has no overall score"
    )
    assert {"median_latency_ms", "estimated_cost_usd"} <= set(metadata["usage"])

    submission = document["submission"]
    assert set(submission) == SUBMISSION_KEYS, (
        f"unexpected submission keys in {path.name}"
    )
    assert submission["submitted_by"] and isinstance(submission["submitted_by"], str)
    assert submission["source"].startswith("https://github.com/WordPress/wp-bench/")
    assert (
        isinstance(submission["reported_at"], str)
        and len(submission["reported_at"]) == 10
    )
    assert set(submission["tests"]) == {"passed", "total"}
