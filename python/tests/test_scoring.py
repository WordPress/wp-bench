from __future__ import annotations

from typing import Any, Dict

from wp_bench.core import BenchmarkRunner
from wp_bench.datasets import ExecutionTest


def _make_test(
    static_checks: Dict[str, Any] | None = None,
    runtime_checks: Dict[str, Any] | None = None,
) -> ExecutionTest:
    return ExecutionTest(
        id="e-test-001",
        suite="wp-core-v1",
        prompt="Do something.",
        expected_behavior="Reviewer contract: does something observable.",
        test_type="execution",
        category="hooks",
        difficulty="intermediate",
        requirements=[],
        static_checks=static_checks or {},
        runtime_checks=runtime_checks or {},
        reference_solution=None,
        metadata={},
    )


def test_correctness_averages_static_and_runtime_dimensions() -> None:
    test = _make_test(
        static_checks={"required_patterns": [{"pattern": "add_filter", "weight": 1.0}]},
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    raw = {
        "static": {"score": 0.5},
        "runtime": {"score": 1.0, "details": {"total_weight": 1.0}},
    }

    assert BenchmarkRunner._score_correctness(raw, test) == 0.75


def test_correctness_uses_only_runtime_when_no_static_checks() -> None:
    test = _make_test(
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    # Runtime returns 1.0 for absent static checks, which must NOT inflate the score.
    raw = {
        "static": {"score": 1.0},
        "runtime": {"score": 0.4, "details": {"total_weight": 1.0}},
    }

    assert BenchmarkRunner._score_correctness(raw, test) == 0.4


def test_correctness_uses_only_static_when_no_runtime_checks() -> None:
    test = _make_test(
        static_checks={"required_patterns": [{"pattern": "esc_html", "weight": 1.0}]},
    )
    # Runtime returns 0.0 when no assertions are defined; it must be ignored here.
    raw = {"static": {"score": 0.8}, "runtime": {"score": 0.0}}

    assert BenchmarkRunner._score_correctness(raw, test) == 0.8


def test_correctness_respects_forbidden_pattern_hard_fail() -> None:
    test = _make_test(
        static_checks={"forbidden_patterns": [{"pattern": "eval\\(", "severity": "error"}]},
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    # Static hard-failed to 0.0 via a forbidden pattern; runtime passed fully.
    raw = {
        "static": {"score": 0.0},
        "runtime": {"score": 1.0, "details": {"total_weight": 1.0}},
    }

    assert BenchmarkRunner._score_correctness(raw, test) == 0.5


def test_correctness_hard_zeroes_on_crash_despite_perfect_static() -> None:
    test = _make_test(
        static_checks={"required_patterns": [{"pattern": "add_filter", "weight": 1.0}]},
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    # Static is perfect, but the code crashed before any assertion ran
    # (runtime accumulated no weight). Static must not rescue unrunnable code.
    raw = {
        "static": {"score": 1.0},
        "runtime": {
            "score": 0.0,
            "details": {
                "assertions": [{"type": "fatal_error", "passed": False}],
                "total_weight": 0.0,
            },
        },
    }

    assert BenchmarkRunner._score_correctness(raw, test) == 0.0


def test_correctness_crash_detected_by_error_assertion_type() -> None:
    test = _make_test(
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )
    # An execution_error entry signals a crash even if some weight accumulated.
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

    assert BenchmarkRunner._score_correctness(raw, test) == 0.0


def test_correctness_keeps_partial_credit_when_code_runs() -> None:
    test = _make_test(
        runtime_checks={
            "assertions": [
                {"type": "hook_registered", "target": "x"},
                {"type": "hook_registered", "target": "y"},
            ]
        },
    )
    # Code ran fine but only half the assertions passed: partial credit stays.
    raw = {
        "runtime": {
            "score": 0.5,
            "details": {
                "assertions": [
                    {"type": "hook_registered", "passed": True, "weight": 1.0},
                    {"type": "hook_registered", "passed": False, "weight": 1.0},
                ],
                "total_weight": 2.0,
            },
        },
    }

    assert BenchmarkRunner._score_correctness(raw, test) == 0.5


def test_correctness_returns_zero_for_empty_raw() -> None:
    test = _make_test(
        runtime_checks={"assertions": [{"type": "hook_registered", "target": "x"}]},
    )

    assert BenchmarkRunner._score_correctness({}, test) == 0.0
