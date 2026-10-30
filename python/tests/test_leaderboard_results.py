"""Shape checks for the per-model leaderboard files (single-model harness output)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

RESULTS_DIR = Path(__file__).resolve().parents[2] / "leaderboard" / "results"
SCORE_KEYS = {
    "correctness",
    "execution_pass_rate",
    "runtime",
    "static_policy_pass_rate",
    "overall",
}
USAGE_KEYS = {"estimated_cost_usd", "median_latency_ms"}


def result_files() -> list[Path]:
    return sorted(RESULTS_DIR.glob("*.json"))


def test_results_folder_is_not_empty() -> None:
    assert result_files(), f"no leaderboard files in {RESULTS_DIR}"


@pytest.mark.parametrize("path", result_files(), ids=lambda p: p.name)
def test_result_file_is_single_model_output(path: Path) -> None:
    payload = json.loads(path.read_text())
    assert set(payload) == {"metadata", "results"}, (
        f"{path.name}: expected harness output shape"
    )
    assert isinstance(payload["results"], list)

    metadata = payload["metadata"]
    model = metadata["model"]["name"]
    assert model and isinstance(model, str)
    assert path.name == model.replace("/", "__") + ".json", (
        f"{path.name} must be named after its model id ({model})"
    )
    assert metadata.get("mode", "model") == "model", (
        f"{path.name}: reference runs are not entries"
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
    assert USAGE_KEYS <= set(metadata["usage"]), f"{path.name} is missing usage keys"
    for record in payload["results"]:
        assert record["model"]["name"] == model, (
            f"{path.name}: record for another model"
        )
