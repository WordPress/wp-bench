"""Deterministic, stratified test selection for limited runs.

When a run is limited (``run.limit``), tests are selected with a seeded,
category/difficulty-stratified strategy instead of "first N by file
order". First-N overrepresents whichever files sort first, which makes
quick provider comparisons biased and misleading. Seeded selection keeps
limited runs deterministic (same seed = same subset), tunable (different
seed = different subset), and representative (round-robin across
category/difficulty groups).
"""
from __future__ import annotations

from typing import Any


def select_tests(
    tests: list[Any],
    *,
    test_ids: list[str],
    categories: list[str] | None,
) -> list[Any]:
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
