"""Tests for runtime-primary execution scoring (SCORING_VERSION 3.0)."""
from __future__ import annotations

from typing import Any

from wp_bench.core import BenchmarkRunner
from wp_bench.datasets import ExecutionTest
from wp_bench.scoring import ScoreAggregator


def _make_test(
    static_checks: dict[str, Any] | None = None,
    runtime_checks: dict[str, Any] | None = None,
) -> ExecutionTest:
    return ExecutionTest(
        id="e-test-001",
        suite="wp-core-v1",
        prompt="Do something.",
        expected_behavior="Reviewer contract: does something observable.",
        category="hooks",
        requirements=[],
        test_function=None,
        static_checks=static_checks or {},
        runtime_checks=runtime_checks or {},
        reference_solution=None,
        metadata={},
    )


def _both_dimensions_test() -> ExecutionTest:
    return _make_test(
        static_checks={"required_patterns": [{"pattern": "add_filter", "weight": 1.0}]},
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )


def test_runtime_pass_static_required_miss_still_passes() -> None:
    """Valid alternate implementations are not punished by regex misses."""
    test = _both_dimensions_test()
    raw = {
        "static": {"score": 0.5, "details": {"forbidden": []}},
        "runtime": {"score": 1.0, "details": {"total_weight": 1.0}},
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is True
    assert scores["correctness"] == 1.0
    assert scores["runtime"] == 1.0
    assert scores["static"] == 0.5  # diagnostic preserved
    assert scores["static_policy_pass"] is True


def test_runtime_fail_static_match_does_not_pass() -> None:
    """Regex matches cannot rescue code whose behavior is wrong."""
    test = _both_dimensions_test()
    raw = {
        "static": {"score": 1.0, "details": {"forbidden": []}},
        "runtime": {"score": 0.0, "details": {"total_weight": 1.0}},
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.0
    assert scores["runtime"] == 0.0


def test_partial_runtime_gives_partial_correctness_but_no_pass() -> None:
    test = _both_dimensions_test()
    raw = {
        "static": {"score": 1.0, "details": {"forbidden": []}},
        "runtime": {"score": 0.5, "details": {"total_weight": 2.0}},
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.5
    assert scores["runtime"] == 0.5


def test_forbidden_static_error_blocks_pass() -> None:
    """A hard policy guardrail failure fails the task even with perfect runtime."""
    test = _make_test(
        static_checks={"forbidden_patterns": [{"pattern": "eval\\(", "severity": "error"}]},
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    raw = {
        "static": {
            "score": 0.0,
            "details": {
                "forbidden": [
                    {"pattern": "eval\\(", "found": True, "severity": "error"}
                ],
                "failure_reason": "Forbidden pattern found: eval(",
            },
        },
        "runtime": {"score": 1.0, "details": {"total_weight": 1.0}},
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["static_policy_pass"] is False
    assert scores["execution_pass"] is False


def test_forbidden_warning_severity_does_not_block_pass() -> None:
    """Only severity=error forbidden patterns are guardrail failures."""
    test = _make_test(
        static_checks={"forbidden_patterns": [{"pattern": "extract\\(", "severity": "warning"}]},
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    raw = {
        "static": {
            "score": 1.0,
            "details": {
                "forbidden": [
                    {"pattern": "extract\\(", "found": True, "severity": "warning"}
                ],
            },
        },
        "runtime": {"score": 1.0, "details": {"total_weight": 1.0}},
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["static_policy_pass"] is True
    assert scores["execution_pass"] is True


def test_runtime_crash_forces_execution_fail() -> None:
    """Crash handling is preserved: static cannot rescue unrunnable code."""
    test = _both_dimensions_test()
    raw = {
        "static": {"score": 1.0, "details": {"forbidden": []}},
        "runtime": {
            "score": 0.0,
            "details": {
                "assertions": [{"type": "fatal_error", "passed": False}],
                "total_weight": 0.0,
            },
        },
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.0
    assert scores["runtime"] == 0.0


def test_crash_detected_by_error_assertion_type() -> None:
    test = _make_test(
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    raw = {
        "runtime": {
            "score": 0.5,
            "details": {
                "assertions": [
                    {"type": "hook_registered", "passed": True, "weight": 1.0},
                    {"type": "execution_error", "passed": False},
                ],
                "total_weight": 1.0,
            },
        },
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.0


def test_static_only_test_scored_on_static() -> None:
    """Tests without runtime assertions remain gradable on static score."""
    test = _make_test(
        static_checks={"required_patterns": [{"pattern": "esc_html", "weight": 1.0}]},
    )
    raw = {"static": {"score": 1.0, "details": {"forbidden": []}}, "runtime": {"score": 0.0}}

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is True
    assert scores["correctness"] == 1.0
    assert scores["runtime"] is None


def test_static_only_partial_score_no_pass() -> None:
    test = _make_test(
        static_checks={"required_patterns": [{"pattern": "esc_html", "weight": 1.0}]},
    )
    raw = {"static": {"score": 0.8, "details": {"forbidden": []}}}

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.8


def test_empty_raw_scores_zero() -> None:
    test = _both_dimensions_test()

    scores = BenchmarkRunner._score_execution({}, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.0


def test_timeout_raw_scores_zero() -> None:
    """The timeout payload from the environment scores as a failed test."""
    test = _both_dimensions_test()
    raw = {
        "success": False,
        "timeout": True,
        "runtime": {
            "score": 0.0,
            "details": {
                "assertions": [{"type": "timeout", "passed": False}],
                "total_weight": 0,
                "passed_weight": 0,
            },
        },
        "static": {"score": 0.0, "details": {}},
    }

    scores = BenchmarkRunner._score_execution(raw, test)

    assert scores["execution_pass"] is False
    assert scores["correctness"] == 0.0


def test_aggregator_reports_strict_pass_rate() -> None:
    aggregator = ScoreAggregator()
    aggregator.add_execution(
        {"correctness": 1.0, "execution_pass": True, "runtime": 1.0, "static_policy_pass": True}
    )
    aggregator.add_execution(
        {"correctness": 0.5, "execution_pass": False, "runtime": 0.5, "static_policy_pass": True}
    )
    aggregator.add_execution(
        {"correctness": 0.0, "execution_pass": False, "runtime": 1.0, "static_policy_pass": False}
    )

    summary = aggregator.finalize()

    assert summary.execution_pass_rate == round(1 / 3, 4)
    assert summary.runtime == round((1.0 + 0.5 + 1.0) / 3, 4)
    assert summary.static_policy_pass_rate == round(2 / 3, 4)
    assert summary.correctness == 0.5


def test_overall_uses_strict_pass_rate() -> None:
    aggregator = ScoreAggregator()
    aggregator.add_execution(
        {"correctness": 1.0, "execution_pass": True, "runtime": 1.0, "static_policy_pass": True}
    )
    aggregator.add_execution(
        {"correctness": 0.9, "execution_pass": False, "runtime": 0.9, "static_policy_pass": True}
    )

    summary = aggregator.finalize()

    # v3.0: overall is the strict execution pass rate.
    assert summary.overall() == 0.5


def test_overall_is_zero_when_nothing_graded() -> None:
    assert ScoreAggregator().finalize().overall() == 0.0
