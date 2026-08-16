"""Test selection and filtering utilities.

Filters tests from an execution suite based on explicit test IDs and/or
matching category tags. When specific test IDs are provided, they take
precedence. When category filters are provided, tests matching any of the
specified categories are retained. If no filters are supplied, all tests
are returned in their original order.
"""
from __future__ import annotations

from typing import Any


def select_tests(
    tests: Iterable[ExecutionTest],
    *,
    test_ids: Sequence[str] | None = None,
    categories: Sequence[str] | None = None,
    limit: int | None = None,
    seed: int | None = None,
) -> list[ExecutionTest]:
    """Select tests for a run based on IDs or category filters.

    Rules:
    - Explicit ``test_ids`` win: returns only the matching test IDs.
    - Category filtering: if ``categories`` is provided, filters to matching categories.
    - Full run: returns all tests in canonical dataset order.
    """
    if test_ids:
        target_ids = set(test_ids)
        return [t for t in tests if getattr(t, "id", "") in target_ids]
    
    if categories:
        target_categories = set(categories)
        filtered = []
        for t in tests:
            test_category = (getattr(t, "category", ""))
            if test_category in target_categories:
                filtered.append(t)
        return filtered
    
    return tests

def selected_test_ids(tests: list[Any]) -> list[str]:
    """IDs of a selection, for dry-run output and result metadata."""
    return [test.id for test in tests]
