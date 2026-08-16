"""Tests for durable result artifacts: a crashed run keeps its graded records."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from wp_bench.results_io import RecordStream, open_stream, timestamped_path


def _ids(path: Path) -> list[str]:
    return [json.loads(line)["test_id"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_are_readable_before_the_run_finishes(tmp_path: Path) -> None:
    """The whole point: a run that dies mid-way leaves graded tests on disk,
    under a name that says the run never finished."""
    stream = RecordStream(tmp_path / "results.jsonl")
    stream.write({"test_id": "e-two"})
    stream.write({"test_id": "e-one"})

    # No close() and no finalize() — the run was killed in flight.
    assert _ids(tmp_path / "results.jsonl.partial") == ["e-two", "e-one"]
    assert not (tmp_path / "results.jsonl").exists()


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
    # The live file is retired and no staging file is left behind.
    assert list(tmp_path.iterdir()) == [tmp_path / "results.jsonl"]


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

    assert _ids(tmp_path / "results.jsonl.partial") == ["e-one", "e-two"]
    assert not path.exists()
    assert not (tmp_path / "results.jsonl.tmp").exists()


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


def _multi_model_config(tmp_path: Path, names: list[str]):
    from wp_bench.config import (
        DatasetConfig,
        GraderConfig,
        HarnessConfig,
        ModelConfig,
        OutputConfig,
        RunConfig,
    )

    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        models=[ModelConfig(name=name) for name in names],
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "multi.json", jsonl_path=tmp_path / "multi.jsonl"),
    )


def _run_multi_model(monkeypatch, tmp_path: Path, names: list[str]):
    """Drive the real MultiModelRunner over one test per model."""
    from conftest import fake_generation

    from wp_bench.core import MultiModelRunner
    from wp_bench.datasets import ExecutionTest
    from wp_bench.environment import ExecutionResult
    from wp_bench.models import ModelInterface

    test = ExecutionTest(
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
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def setup(self, *, capture_baseline: bool = True, worker_count: int = 1) -> None: ...
        def reset(self, worker: int = 0) -> None: ...

        def execute_artifact(self, artifact: object, spec: dict, worker: int = 0) -> ExecutionResult:
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

    monkeypatch.setattr("wp_bench.core.WordPressEnvironment", FakeEnvironment)
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [test])
    monkeypatch.setattr(
        ModelInterface, "generate_with_metadata", lambda self, prompt: fake_generation("init")
    )
    runner = MultiModelRunner(_multi_model_config(tmp_path, names))
    runner.run()
    return runner


def test_every_model_streams_into_one_run_artifact(monkeypatch, tmp_path: Path) -> None:
    """MultiModelRunner shares one stream across its per-model runners, so a
    multi-model run that dies keeps the models already graded — its combined
    JSON payload is only assembled after the last model finishes."""
    _run_multi_model(monkeypatch, tmp_path, ["model-a", "model-b"])

    artifact = next(tmp_path.glob("multi_*.jsonl"))
    streamed = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]
    assert sorted(record["model"]["name"] for record in streamed) == ["model-a", "model-b"]
    assert not list(tmp_path.glob("*.partial"))


def test_finalize_keeps_the_live_file_when_it_would_shrink(tmp_path: Path) -> None:
    """A caller that finalizes with fewer records than were streamed has lost
    track of some; the live file keeps them recoverable instead of deleting
    them. Config rejects the duplicate-model case that used to reach here, so
    this pins the invariant itself."""
    stream = RecordStream(tmp_path / "results.jsonl")
    stream.write({"test_id": "e-one"})
    stream.write({"test_id": "e-two"})

    stream.finalize([{"test_id": "e-one"}])

    assert _ids(tmp_path / "results.jsonl") == ["e-one"]
    assert _ids(tmp_path / "results.jsonl.partial") == ["e-one", "e-two"]


def test_duplicate_model_names_are_rejected_before_a_run(tmp_path: Path) -> None:
    """Results are keyed by model name end to end, so two entries sharing one
    would silently drop a graded pass. Rejecting it in config is the root fix."""
    import pytest

    from wp_bench.config import HarnessConfig, ModelConfig

    with pytest.raises(Exception, match="Duplicate model names"):
        HarnessConfig(models=[ModelConfig(name="same"), ModelConfig(name="same")])
