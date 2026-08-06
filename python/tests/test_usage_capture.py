"""Tests for token usage, latency, and cost capture."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import wp_bench.models as models_module
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
from wp_bench.models import ModelInterface
from wp_bench.scoring import UsageAggregator


def _response(with_usage: bool = True) -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    return SimpleNamespace(
        choices=[SimpleNamespace(message={"content": "answer"})],
        id="resp-1",
        usage=usage if with_usage else None,
    )


def test_generate_with_metadata_extracts_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_module, "completion", lambda **kwargs: _response())
    monkeypatch.setattr(models_module, "completion_cost", lambda response: 0.0123)
    model = ModelInterface(ModelConfig(name="gpt-4o-mini"))

    generation = model.generate_with_metadata("hello")

    assert generation.prompt_tokens == 100
    assert generation.completion_tokens == 50
    assert generation.total_tokens == 150
    assert generation.cost_usd == 0.0123
    assert generation.latency_ms >= 0


def test_generate_with_metadata_handles_missing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models_module, "completion", lambda **kwargs: _response(with_usage=False))
    monkeypatch.setattr(models_module, "completion_cost", lambda response: 0.0)
    model = ModelInterface(ModelConfig(name="gpt-4o-mini"))

    generation = model.generate_with_metadata("hello")

    assert generation.prompt_tokens is None
    assert generation.completion_tokens is None
    assert generation.total_tokens is None


def test_cost_estimation_failure_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_cost(response: object) -> float:
        raise RuntimeError("no pricing for model")

    monkeypatch.setattr(models_module, "completion", lambda **kwargs: _response())
    monkeypatch.setattr(models_module, "completion_cost", broken_cost)
    model = ModelInterface(ModelConfig(name="local/unknown-model"))

    generation = model.generate_with_metadata("hello")

    assert generation.text == "answer"
    assert generation.cost_usd is None


def test_usage_dict_shape_matches_canonical_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models_module, "completion", lambda **kwargs: _response())
    monkeypatch.setattr(models_module, "completion_cost", lambda response: 0.01)
    model = ModelInterface(ModelConfig(name="gpt-4o-mini"))

    usage = model.generate_with_metadata("hello").usage_dict()

    assert set(usage.keys()) == {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "latency_ms",
    }


def test_usage_aggregator_sums_and_percentiles() -> None:
    aggregator = UsageAggregator()
    for latency in (100.0, 200.0, 300.0, 400.0, 500.0):
        aggregator.add(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.01,
                "latency_ms": latency,
            }
        )

    summary = aggregator.summary()

    assert summary["prompt_tokens"] == 50
    assert summary["completion_tokens"] == 25
    assert summary["total_tokens"] == 75
    assert summary["estimated_cost_usd"] == 0.05
    assert summary["median_latency_ms"] == 300.0
    assert summary["p95_latency_ms"] == 500.0


def test_usage_aggregator_tolerates_missing_fields() -> None:
    aggregator = UsageAggregator()
    aggregator.add({"latency_ms": 100.0})
    aggregator.add(None)
    aggregator.add({"prompt_tokens": None, "cost_usd": None})

    summary = aggregator.summary()

    assert summary["total_tokens"] == 0
    assert summary["estimated_cost_usd"] is None
    assert summary["median_latency_ms"] == 100.0


def test_result_record_contains_usage_and_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test = ExecutionTest(
        id="e-one",
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="hooks",
        difficulty="basic",
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution=None,
        metadata={},
    )
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="gpt-4o-mini"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [test])
    monkeypatch.setattr(models_module, "completion", lambda **kwargs: _response())
    monkeypatch.setattr(models_module, "completion_cost", lambda response: 0.002)
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    raw = {
        "success": True,
        "static": {"score": 1.0, "details": {"total_weight": 1}},
        "runtime": {"score": 1.0, "details": {"total_weight": 1}},
    }
    runner.environment.execute_artifact = lambda artifact, verification_spec: ExecutionResult(  # type: ignore[method-assign]
        success=True, raw=raw, stdout="", stderr=""
    )

    payload = runner.run()

    record = payload["results"][0]
    assert record["usage"]["prompt_tokens"] == 100
    assert record["usage"]["cost_usd"] == 0.002
    assert record["usage"]["latency_ms"] >= 0
    assert record["model_call"]["retry_count"] == 0
    assert record["model_call"]["provider_response_id"] == "resp-1"
    run_usage = payload["metadata"]["usage"]
    assert run_usage["total_tokens"] == 150
    assert run_usage["estimated_cost_usd"] == 0.002
