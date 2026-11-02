"""Tests flagged requires_multisite run on a separately captured network baseline."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import fake_generation

from wp_bench.config import (
    DatasetConfig,
    GraderConfig,
    HarnessConfig,
    ModelConfig,
    OutputConfig,
    RunConfig,
)
from wp_bench.core import BenchmarkRunner, MultiModelRunner, requires_multisite
from wp_bench.datasets import ExecutionTest
from wp_bench.environment import ExecutionResult, ProcessResult, WordPressEnvironment

SINGLE = base64.b64encode(b"SQLite format 3\0single").decode()
NETWORK = base64.b64encode(b"SQLite format 3\0network").decode()
SINGLE_CONFIG = "<?php // single-site"
NETWORK_CONFIG = "<?php define( 'MULTISITE', true );"


def _environment() -> tuple[WordPressEnvironment, list[tuple[list[str], str | None]]]:
    """A Docker environment whose container commands are scripted."""
    environment = WordPressEnvironment(GraderConfig())
    calls: list[tuple[list[str], str | None]] = []
    state = {"multisite": False}

    def fake_exec(command: list[str], *, stdin: str | None = None, **kwargs: Any):
        calls.append((command, stdin))
        if command == ["cat", "wp-config.php"]:
            return (NETWORK_CONFIG if state["multisite"] else SINGLE_CONFIG), "", 0, False
        script = command[2] if command[:2] == ["sh", "-c"] else ""
        if "wp core multisite-install" in script:
            state["multisite"] = True
            return NETWORK, "", 0, False
        if "wp core install" in script:
            return SINGLE, "", 0, False
        return "", "", 0, False

    environment._exec = fake_exec  # type: ignore[method-assign]
    environment._resolve_image = lambda: None  # type: ignore[method-assign]
    environment._start_isolated_container = lambda **kwargs: None  # type: ignore[method-assign]
    environment._run_process = lambda *args, **kwargs: ProcessResult("", "", 0, False)  # type: ignore[method-assign]
    environment._wait_for_container = lambda: None  # type: ignore[method-assign]
    environment._require_sqlite = lambda: None  # type: ignore[method-assign]
    return environment, calls


def _scripts(calls: list[tuple[list[str], str | None]]) -> list[str]:
    return [command[2] for command, _ in calls if command[:2] == ["sh", "-c"]]


def test_default_setup_captures_only_the_single_site_baseline() -> None:
    environment, calls = _environment()
    environment.setup()
    scripts = _scripts(calls)
    assert len(scripts) == 1
    assert "wp core install" in scripts[0]
    assert environment._multisite_baseline is None
    with pytest.raises(RuntimeError, match="No clean multisite baseline"):
        environment.reset(multisite=True)


def test_multisite_setup_captures_single_site_first_then_the_network() -> None:
    environment, calls = _environment()
    environment.setup(multisite=True)
    scripts = _scripts(calls)
    assert len(scripts) == 2
    assert "wp core install" in scripts[0]
    network = scripts[1]
    assert network.index("clear") < network.index("wp core multisite-install") < network.index("export")
    assert "--url=http://localhost:8888" in network
    assert "--subdomains" not in network
    # The single-site configuration is read before the network install
    # rewrites wp-config.php, so each baseline keeps its own configuration.
    assert (environment._baseline, environment._site_config) == (SINGLE, SINGLE_CONFIG)
    assert (environment._multisite_baseline, environment._multisite_config) == (
        NETWORK,
        NETWORK_CONFIG,
    )


def test_reset_restores_the_requested_baseline() -> None:
    environment, calls = _environment()
    environment.setup(multisite=True)
    calls.clear()

    environment.reset()
    environment.reset(multisite=True)

    single, network = calls
    assert single[0][2].endswith("prepare && wp core is-installed")
    assert json.loads(single[1] or "") == {"config": SINGLE_CONFIG, "database": SINGLE}
    assert network[0][2].endswith("prepare && wp core is-installed --network")
    assert json.loads(network[1] or "") == {"config": NETWORK_CONFIG, "database": NETWORK}


def test_multisite_needs_a_captured_baseline() -> None:
    environment, calls = _environment()
    with pytest.raises(RuntimeError, match="reset_per_test"):
        environment.setup(capture_baseline=False, multisite=True)
    assert calls == []


def _test(test_id: str, *, multisite: bool = False) -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="multisite" if multisite else "general",
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution="function ref() { return true; }",
        metadata={"requires_multisite": True} if multisite else {},
    )


def _result(passed: bool) -> ExecutionResult:
    score = 1.0 if passed else 0.0
    raw = {
        "success": passed,
        "static": {"score": 1.0, "details": {"total_weight": 1}},
        "runtime": {"score": score, "details": {"total_weight": 1}},
    }
    return ExecutionResult(success=passed, raw=raw, stdout="", stderr="")


class SpyEnvironment:
    """Records which baseline each setup and reset asks for."""

    image_id = "sha256:test"

    def __init__(self, *, passing: bool = True) -> None:
        self.calls: list[str] = []
        self.passing = passing

    def release(self) -> None: ...
    def close(self) -> None: ...

    def setup(self, *, capture_baseline: bool = True, multisite: bool = False) -> None:
        self.calls.append("setup+multisite" if multisite else "setup")

    def reset(self, *, multisite: bool = False) -> None:
        self.calls.append("reset:multisite" if multisite else "reset:single")

    def execute_artifact(self, artifact: object, verification_spec: dict) -> ExecutionResult:
        return _result(self.passing)


def _config(tmp_path: Path, **run_overrides: Any) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(),
        run=RunConfig(**run_overrides),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )


def _runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tests: list[ExecutionTest], spy: SpyEnvironment, **run: Any) -> BenchmarkRunner:
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: tests)
    runner = BenchmarkRunner(_config(tmp_path, **run))
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )
    return runner


def test_requires_multisite_reads_only_an_explicit_true_flag() -> None:
    assert requires_multisite(_test("e-a", multisite=True))
    assert not requires_multisite(_test("e-b"))
    flagged_as_string = _test("e-c")
    flagged_as_string.metadata = {"requires_multisite": "true"}
    assert not requires_multisite(flagged_as_string)


def test_single_site_selection_never_captures_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = SpyEnvironment()
    tests = [_test("e-one"), _test("e-two"), _test("e-network", multisite=True)]
    runner = _runner(monkeypatch, tmp_path, tests, spy, test_ids=["e-one", "e-two"])
    runner.run()
    assert spy.calls == ["setup", "reset:single", "reset:single"]


def test_each_test_restores_its_own_site_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = SpyEnvironment()
    tests = [_test("e-one"), _test("e-network", multisite=True), _test("e-two")]
    runner = _runner(monkeypatch, tmp_path, tests, spy)
    runner.run()
    assert spy.calls[0] == "setup+multisite"
    assert sorted(spy.calls[1:]) == ["reset:multisite", "reset:single", "reset:single"]


def test_reference_check_routes_multisite_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = SpyEnvironment()
    tests = [_test("e-network", multisite=True)]
    runner = _runner(monkeypatch, tmp_path, tests, spy, check_reference_solution=True)
    runner.run()
    assert spy.calls == ["setup+multisite", "reset:multisite"]


def test_exploit_audit_resets_every_candidate_to_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = SpyEnvironment(passing=False)
    test = _test("e-network", multisite=True)
    test.test_function = "wpbp_network(): array"
    test.exploit_solutions = ["function wpbp_network(): array { return array( 1 ); }"]
    runner = _runner(monkeypatch, tmp_path, [test], spy, check_exploits=True)
    runner.run()
    assert spy.calls[0] == "setup+multisite"
    assert len(spy.calls) > 2
    assert set(spy.calls[1:]) == {"reset:multisite"}


def test_multi_model_runs_capture_the_network_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tests = [_test("e-one"), _test("e-network", multisite=True)]
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: tests)
    monkeypatch.setattr(
        "wp_bench.core.ModelInterface.generate_with_metadata",
        lambda self, prompt: fake_generation("```php\ncode\n```"),
    )
    config = _config(tmp_path)
    config.model = None
    config.models = [ModelConfig(name="model-a"), ModelConfig(name="model-b")]
    runner = MultiModelRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    runner.run()
    assert spy.calls.count("setup+multisite") == 1
    assert spy.calls.count("reset:multisite") == 2
    assert spy.calls.count("reset:single") == 2


def test_shared_runtime_refuses_multisite_tests_before_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = SpyEnvironment()
    tests = [_test("e-one"), _test("e-network", multisite=True)]
    runner = _runner(monkeypatch, tmp_path, tests, spy, execution_isolation="none")
    with pytest.raises(ValueError, match="requires_multisite"):
        runner.run()
    assert spy.calls == []
