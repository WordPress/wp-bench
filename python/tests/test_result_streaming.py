"""Tests for durable result artifacts: a crashed run keeps its graded records."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from wp_bench.results_io import RecordStream, open_stream, timestamped_path


def _ids(path: Path) -> list[str]:
    return [json.loads(line)["test_id"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_are_readable_before_the_run_finishes(tmp_path: Path) -> None:
    """The whole point: a run that dies mid-way leaves graded tests on disk."""
    stream = RecordStream(tmp_path / "results.jsonl")
    stream.write({"test_id": "e-two"})
    stream.write({"test_id": "e-one"})

    # No close() and no finalize() — the run was killed in flight.
    assert _ids(tmp_path / "results.jsonl") == ["e-two", "e-one"]


def test_no_file_when_nothing_was_graded(tmp_path: Path) -> None:
    """A run that fails before grading anything leaves no empty artifact."""
    stream = RecordStream(tmp_path / "results.jsonl")
    stream.close()
    stream.finalize([])
    assert list(tmp_path.iterdir()) == []


def test_disabled_stream_writes_nothing(tmp_path: Path) -> None:
    """jsonl_path: null keeps the old behavior of no line-oriented output."""
    stream = RecordStream(None)
    stream.write({"test_id": "e-one"})
    stream.finalize([{"test_id": "e-one"}])
    stream.close()
    assert list(tmp_path.iterdir()) == []


def test_finalize_replaces_the_live_file_with_the_given_order(tmp_path: Path) -> None:
    """In flight the file is completion-ordered; the finished artifact carries
    the run's canonical order, so result diffs stay stable across runs."""
    stream = RecordStream(tmp_path / "results.jsonl")
    stream.write({"test_id": "e-two"})
    stream.write({"test_id": "e-one"})

    stream.finalize([{"test_id": "e-one"}, {"test_id": "e-two"}])

    assert _ids(tmp_path / "results.jsonl") == ["e-one", "e-two"]
    assert list(tmp_path.iterdir()) == [tmp_path / "results.jsonl"]  # no .tmp left behind


def test_finalize_never_destroys_streamed_records_it_cannot_replace(tmp_path: Path) -> None:
    """The replacement is staged beside the live file, so an interrupted
    finalize leaves the streamed records intact rather than a truncated file."""
    path = tmp_path / "results.jsonl"
    stream = RecordStream(path)
    stream.write({"test_id": "e-one"})
    stream.write({"test_id": "e-two"})

    class Unserializable:
        """orjson cannot encode this, so the rewrite dies partway."""

    try:
        stream.finalize([{"test_id": "e-one"}, {"test_id": Unserializable()}])
    except TypeError:
        pass

    assert _ids(path) == ["e-one", "e-two"]


def test_run_artifacts_share_one_timestamp() -> None:
    """The JSON and JSONL of one run must be correlatable by filename."""
    moment = datetime(2026, 8, 5, 14, 30, 52, tzinfo=timezone.utc)
    assert timestamped_path(Path("out/results.json"), moment).name == "results_20260805_143052.json"
    assert timestamped_path(Path("out/results.jsonl"), moment).name == "results_20260805_143052.jsonl"


def test_open_stream_disables_itself_without_a_configured_path() -> None:
    moment = datetime(2026, 8, 5, 14, 30, 52, tzinfo=timezone.utc)
    assert open_stream(None, moment).path is None
    assert open_stream(Path("out/results.jsonl"), moment).path == Path(
        "out/results_20260805_143052.jsonl"
    )


def test_every_model_streams_into_one_run_artifact(tmp_path: Path) -> None:
    """MultiModelRunner shares one stream across its per-model runners, so a
    multi-model run that dies keeps the models already graded — its combined
    JSON payload is only assembled after every model finishes."""
    from conftest import fake_generation

    from wp_bench.config import (
        DatasetConfig,
        GraderConfig,
        HarnessConfig,
        ModelConfig,
        OutputConfig,
        RunConfig,
    )
    from wp_bench.core import SingleModelRunner
    from wp_bench.datasets import ExecutionTest
    from wp_bench.environment import ExecutionResult

    def execution_test() -> ExecutionTest:
        return ExecutionTest(
            id="e-one",
            suite="wp-core-v1",
            prompt="Prompt",
            expected_behavior="expected",
            category="hooks",
            difficulty="basic",
            requirements=["Requirement"],
            test_function=None,
            static_checks={},
            runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
            reference_solution="function ref() { return true; }",
            metadata={},
        )

    class FakeEnvironment:
        def setup(self) -> None: ...
        def reset(self) -> None: ...

        def execute_artifact(self, artifact: object, spec: dict) -> ExecutionResult:
            return ExecutionResult(
                success=True,
                stdout="out",
                stderr="",
                raw={
                    "success": True,
                    "static": {"score": 1.0, "details": {"total_weight": 1}},
                    "runtime": {"score": 1.0, "details": {"total_weight": 1}},
                },
            )

    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        models=[ModelConfig(name="model-a"), ModelConfig(name="model-b")],
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "multi.json", jsonl_path=tmp_path / "multi.jsonl"),
    )
    stream = RecordStream(tmp_path / "multi.jsonl")
    environment = FakeEnvironment()

    for model_config in config.get_models():
        runner = SingleModelRunner(
            config=config,
            model_config=model_config,
            environment=environment,  # type: ignore[arg-type]
            tests=[execution_test()],
            stream=stream,
        )
        runner.model.generate_with_metadata = lambda prompt: fake_generation("init")  # type: ignore[method-assign]
        runner.run()

    streamed = [json.loads(line) for line in (tmp_path / "multi.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["model"]["name"] for record in streamed] == ["model-a", "model-b"]
    assert all(record["test_id"] == "e-one" for record in streamed)
