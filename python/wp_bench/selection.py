"""Deterministic, stratified test selection for limited runs.

When a run is limited (``run.limit``), tests are selected with a seeded,
category-stratified strategy instead of "first N by file
order". First-N overrepresents whichever files sort first, which makes
quick provider comparisons biased and misleading. Seeded selection keeps
limited runs deterministic (same seed = same subset), tunable (different
seed = different subset), and representative (round-robin across
categories).
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any


def select_tests(
    tests: list[Any],
    *,
    limit: int | None,
    test_ids: list[str],
    seed: int,
) -> list[Any]:
    """Select tests for a run, deterministically.

    Rules:
    - Explicit ``test_ids`` win: the tests are returned in dataset order,
      unaffected by limit or seed (filtering by ID happens upstream).
    - No limit: all tests in dataset order (canonical full-run behavior).
    - Limit: seeded stratified sampling. Tests are grouped by
      category; each group is shuffled with the seed and
      groups are drained round-robin (in deterministic group order) until
      the limit is reached, so small subsets still touch as many groups
      as possible. The final selection is sorted by test id for stable
      output.
    """
    if test_ids:
        return tests
    if limit is None or limit >= len(tests):
        return tests

    groups: dict[Any, list[Any]] = defaultdict(list)
    for test in tests:
        key = getattr(test, "category", "")
        groups[key].append(test)

    rng = random.Random(seed)
    ordered_keys = sorted(groups.keys())
    for key in ordered_keys:
        rng.shuffle(groups[key])

    selected: list[Any] = []
    while len(selected) < limit:
        progressed = False
        for key in ordered_keys:
            if len(selected) >= limit:
                break
            if groups[key]:
                selected.append(groups[key].pop())
                progressed = True
        if not progressed:
            break

    return sorted(selected, key=lambda test: test.id)


def selected_test_ids(tests: list[Any]) -> list[str]:
    """IDs of a selection, for dry-run output and result metadata."""
    return [test.id for test in tests]
