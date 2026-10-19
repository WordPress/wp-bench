"""Tests for seeded, stratified test selection on limited runs."""
from __future__ import annotations

from wp_bench.datasets import ExecutionTest
from wp_bench.selection import select_tests, selected_test_ids


def _test(test_id: str, category: str) -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category=category,
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={},
        reference_solution=None,
        metadata={},
    )


def _fixture() -> list[ExecutionTest]:
    """Tests ordered by category so first-N would only see 'aaa'."""
    tests = []
    for i in range(10):
        tests.append(_test(f"e-aaa-{i:03d}", "aaa"))
    for i in range(10):
        tests.append(_test(f"e-bbb-{i:03d}", "bbb"))
    for i in range(10):
        tests.append(_test(f"e-ccc-{i:03d}", "ccc"))
    return tests


def test_limit_selection_is_seeded_and_deterministic() -> None:
    tests = _fixture()

    first = select_tests(tests, limit=6, test_ids=[], seed=1337)
    second = select_tests(tests, limit=6, test_ids=[], seed=1337)

    assert selected_test_ids(first) == selected_test_ids(second)
    assert len(first) == 6


def test_limit_selection_changes_with_seed() -> None:
    tests = _fixture()

    a = select_tests(tests, limit=6, test_ids=[], seed=1)
    b = select_tests(tests, limit=6, test_ids=[], seed=2)

    assert selected_test_ids(a) != selected_test_ids(b)


def test_selection_is_not_first_n() -> None:
    """A limited subset must not be dominated by the first-sorted category."""
    tests = _fixture()

    selected = select_tests(tests, limit=6, test_ids=[], seed=1337)

    categories = {test.category for test in selected}
    assert categories == {"aaa", "bbb", "ccc"}


def test_stratified_selection_balances_groups() -> None:
    tests = _fixture()

    selected = select_tests(tests, limit=6, test_ids=[], seed=1337)

    per_category = {
        category: sum(1 for t in selected if t.category == category)
        for category in ("aaa", "bbb", "ccc")
    }
    assert per_category == {"aaa": 2, "bbb": 2, "ccc": 2}


def test_explicit_test_ids_ignore_limit_and_seed() -> None:
    tests = [_test("e-aaa-001", "aaa"), _test("e-bbb-001", "bbb")]

    selected = select_tests(tests, limit=1, test_ids=["e-aaa-001", "e-bbb-001"], seed=1)

    assert selected_test_ids(selected) == ["e-aaa-001", "e-bbb-001"]


def test_no_limit_returns_all_in_dataset_order() -> None:
    tests = _fixture()

    selected = select_tests(tests, limit=None, test_ids=[], seed=1337)

    assert selected is tests


def test_limit_larger_than_dataset_returns_all() -> None:
    tests = _fixture()

    selected = select_tests(tests, limit=999, test_ids=[], seed=1337)

    assert selected is tests


def test_limit_smaller_than_group_count_touches_distinct_groups() -> None:
    tests = _fixture()

    selected = select_tests(tests, limit=2, test_ids=[], seed=1337)

    assert len(selected) == 2
    assert len({test.category for test in selected}) == 2


def test_selected_output_is_sorted_by_id() -> None:
    tests = _fixture()

    selected = select_tests(tests, limit=6, test_ids=[], seed=1337)

    ids = selected_test_ids(selected)
    assert ids == sorted(ids)
