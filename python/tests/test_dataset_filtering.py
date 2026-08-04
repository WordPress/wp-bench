from __future__ import annotations

import pytest

from wp_bench.cli import _normalize_test_ids
from wp_bench.datasets import ExecutionTest, filter_tests_by_ids


def _execution_test(test_id: str) -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="general",
        difficulty="basic",
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={},
        reference_solution=None,
        metadata={},
    )


def test_normalize_test_ids_accepts_repeated_and_comma_separated_values() -> None:
    assert _normalize_test_ids(["e-one,e-two", "e-two", " e-three "]) == [
        "e-one",
        "e-two",
        "e-three",
    ]


def test_filter_tests_by_ids_returns_requested_tests() -> None:
    tests = [_execution_test("e-one"), _execution_test("e-two")]

    filtered = filter_tests_by_ids(tests, ["e-two"])

    assert [test.id for test in filtered] == ["e-two"]


def test_filter_tests_by_ids_without_ids_returns_all() -> None:
    tests = [_execution_test("e-one"), _execution_test("e-two")]

    assert filter_tests_by_ids(tests, []) is tests


def test_filter_tests_by_ids_rejects_unknown_ids() -> None:
    tests = [_execution_test("e-one")]

    with pytest.raises(ValueError, match="Unknown test id"):
        filter_tests_by_ids(tests, ["missing"])


def test_zero_selected_tests_fails_loudly() -> None:
    """An empty selection (missing suite, execution-less dataset) must not
    produce a vacuous successful run."""
    import pytest

    from wp_bench.config import HarnessConfig
    from wp_bench.core import _limit_tests

    config = HarnessConfig.model_validate({"dataset": {"source": "local", "name": "wp-core-v1"}})
    with pytest.raises(ValueError, match="No execution tests selected"):
        _limit_tests([], config)


def test_dry_run_zero_selection_fails_loudly(tmp_path) -> None:
    """dry-run must use the guarded selector: a suite with no execution
    tests errors instead of printing a successful zero count."""
    import pytest

    from wp_bench.cli import _select_for_config
    from wp_bench.config import HarnessConfig

    config = HarnessConfig.model_validate({"dataset": {"source": "local", "name": "wp-core-v1"}})
    with pytest.raises(ValueError, match="No execution tests selected"):
        _select_for_config([], config)
