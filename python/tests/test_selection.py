"""Tests for test selection (test_ids and category filtering)."""
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
    """Fixture with grouped test instances."""
    tests = []
    for i in range(10):
        tests.append(_test(f"e-aaa-{i:03d}", "aaa"))
    for i in range(10):
        tests.append(_test(f"e-bbb-{i:03d}", "bbb"))
    for i in range(10):
        tests.append(_test(f"e-ccc-{i:03d}", "ccc"))
    return tests


def test_select_all_by_default() -> None:
    """Without filters, all tests are returned in original dataset order."""
    tests = _fixture()
    selected = select_tests(tests)

    assert selected == tests
    assert len(selected) == 30


def test_select_by_explicit_test_ids() -> None:
    """Explicit test_ids return only the matching subset."""
    tests = _fixture()
    target_ids = ["e-aaa-001", "e-ccc-005"]

    selected = select_tests(tests, test_ids=target_ids)

    assert selected_test_ids(selected) == target_ids


def test_select_by_single_category() -> None:
    """Category filter selects all tests belonging to that category."""
    tests = _fixture()

    selected = select_tests(tests, categories=["bbb"])

    assert len(selected) == 10
    assert all(t.category == "bbb" for t in selected)
    assert selected_test_ids(selected) == [f"e-bbb-{i:03d}" for i in range(10)]


def test_select_by_multiple_categories() -> None:
    """Category filter with multiple entries selects all matching tests."""
    tests = _fixture()

    selected = select_tests(tests, categories=["aaa", "ccc"])

    assert len(selected) == 20
    assert {t.category for t in selected} == {"aaa", "ccc"}


def test_explicit_test_ids_override_categories() -> None:
    """When both test_ids and categories are passed, explicit test_ids take precedence."""
    tests = _fixture()

    selected = select_tests(
        tests,
        test_ids=["e-aaa-001", "e-bbb-002"],
        categories=["ccc"],
    )

    assert selected_test_ids(selected) == ["e-aaa-001", "e-bbb-002"]


def test_select_nonexistent_test_ids_returns_empty() -> None:
    """Filtering by test_ids that do not exist yields an empty list."""
    tests = _fixture()

    selected = select_tests(tests, test_ids=["non-existent-id"])

    assert selected == []


def test_select_nonexistent_category_returns_empty() -> None:
    """Filtering by an unknown category yields an empty list."""
    tests = _fixture()

    selected = select_tests(tests, categories=["non-existent-cat"])

    assert selected == []