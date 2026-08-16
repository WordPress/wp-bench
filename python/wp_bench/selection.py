"""Test selection and filtering utilities.

Filters tests from an execution suite based on explicit test IDs and/or
matching category tags. When specific test IDs are provided, they take
precedence. When category filters are provided, tests matching any of the
specified categories are retained. If no filters are supplied, all tests
are returned in their original order.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .datasets import ExecutionTest


def select_tests(
    tests: Iterable[ExecutionTest],
    *,
    test_ids: Sequence[str] | None = None,
    categories: Sequence[str] | None = None,
    limit: int | None = None,
    seed: int | None = None,
) -> list[ExecutionTest]:
    """Filter tests by explicit test IDs and/or category membership."""
    selected_tests: list[ExecutionTest] = list(tests)

    if test_ids is not None:
        id_set = set(test_ids)
        selected_tests = [t for t in selected_tests if t.id in id_set]

    if categories is not None:
        cat_set = set(categories)
        selected_tests = [
            t for t in selected_tests
            if getattr(t, "category", None) in cat_set
            or (hasattr(t, "categories") and t.categories and any(c in cat_set for c in t.categories))
        ]

    if limit is not None and limit > 0:
        selected_tests = selected_tests[:limit]

    return selected_tests

def selected_test_ids(tests: list[Any]) -> list[str]:
    """IDs of a selection, for dry-run output and result metadata."""
    return [test.id for test in tests]
